"""循环本体的纯逻辑测试：不启浏览器、不调模型。

只测几件"写错了会静默出错"的事：
1. 页面差异提取（用来告诉模型"你刚才那下让页面变了什么"）
2. 循环检测（连续重复 + A B A B 交替）
3. 空白页判定（真实站点 R3 实测：空白页喂给模型 → 它开始编编号）
4. 失败留痕（真实站点 R4 实测：失败任务的 trace 是空数组，等于现场一片空白）

这几个都是实测翻过车的地方，必须有回归测试守着。
"""
from __future__ import annotations

from app.agent.workflow import BrowserAgentWorkflow, _is_blank, _text_delta


class _FakeLLM:
    """最小模型桩：wrkflow 记 trace 时会读 last_usage，给个空 dict 就够。

    为什么不在生产代码里写 getattr(self.llm, "last_usage", {}) 兜底：
    那会把"llm 必须存在"这个前提藏起来，真出了问题反而更难发现。
    测试桩的职责就是把前提补齐。
    """

    last_usage: dict = {}


def make_workflow() -> BrowserAgentWorkflow:
    """只测纯逻辑，session 给 None；llm 给桩，因为记 trace 会读它的用量。"""
    return BrowserAgentWorkflow(session=None, llm=_FakeLLM())  # type: ignore[arg-type]


def test_text_delta_picks_appended_text():
    """最常见形态：动作生效后正文末尾多一行（"提交成功：…"）。"""
    old = "资料填写 姓名 邮箱 城市 提交"
    new = "资料填写 姓名 邮箱 城市 提交 提交成功：姓名=张三"
    assert _text_delta(old, new) == "提交成功：姓名=张三"


def test_text_delta_returns_empty_when_identical():
    assert _text_delta("abc", "abc") == ""


def test_text_delta_handles_middle_insertion():
    """列表中间插了一行（搜索后结果列表变化）也要能挑出来。"""
    old = "共 0 条结果 结果：结束"
    new = "共 1 条结果 结果：USB 集线器 结束"
    delta = _text_delta(old, new)
    assert "USB 集线器" in delta


def test_loop_detects_same_action_three_times():
    wf = make_workflow()
    wf.trace = [{"step": i, "action": "click e6"} for i in range(1, 4)]
    assert wf._is_looping() is True


def test_loop_detects_alternating_actions():
    """T5 实测的"下一页 ↔ 上一页"死循环：只查连续相同是抓不到的。"""
    wf = make_workflow()
    wf.trace = [
        {"step": 1, "action": "click e2"},
        {"step": 2, "action": "click e1"},
        {"step": 3, "action": "click e2"},
        {"step": 4, "action": "click e1"},
    ]
    assert wf._is_looping() is True


def test_loop_not_triggered_by_normal_progress():
    wf = make_workflow()
    wf.trace = [
        {"step": 1, "action": "click e2"},
        {"step": 2, "action": "type e2 <- '张三'"},
        {"step": 3, "action": "select e4 <- 福州"},
    ]
    assert wf._is_looping() is False


# ---------------- 空白页判定 ----------------
def test_blank_when_no_elements_and_no_text():
    """换页瞬间的"纯白页"：元素 0、正文空。必须被识别出来，否则会喂给模型。"""
    assert _is_blank({"elements_total": 0, "text": ""}) is True
    assert _is_blank({"elements_total": 0, "text": "   \n  "}) is True


def test_not_blank_when_only_text_exists():
    """纯文字页（文章正文、结果页）是正常的，不该当成空白去重试。"""
    assert _is_blank({"elements_total": 0, "text": "这是一段正文"}) is False


def test_not_blank_when_elements_exist():
    """有元素但正文被截断成空：也算正常页面。"""
    assert _is_blank({"elements_total": 3, "text": ""}) is False


def test_blank_handles_none_snapshot():
    assert _is_blank(None) is True


# ---------------- 失败留痕 ----------------
def test_rejected_action_records_raw_output():
    """校验失败时要留下模型的**原始输出**，不能只留错误原因。

    真实站点 R4：错误原因只说"e24 是 link，不能输入文字"，
    但看不出模型到底想干什么。原始 JSON 才能区分"选错元素"和"误解页面结构"。
    """
    wf = make_workflow()
    wf.step_no = 1
    wf.current_url = "https://example.com/"
    wf.snapshot = {"elements_total": 42}

    wf._record_rejected(
        "e24 是 link，不能往它里面输入文字",
        {"thought": "我要在搜索框输入关键词", "action": "type", "target": "e24", "text": "map"},
    )

    assert len(wf.trace) == 1
    row = wf.trace[0]
    assert row["ok"] is False
    assert row["step"] == 1
    assert row["elements_total"] == 42
    # 原始输出要留下，包括 thought 和 target
    assert "type" in row["action"] and "e24" in row["action"]
    assert row["thought"] == "我要在搜索框输入关键词"
    assert "link" in row["error"]


def test_rejected_action_handles_unparsable_output():
    """模型连 JSON 都没给出来（payload=None）时也要留痕，不能崩。"""
    wf = make_workflow()
    wf.step_no = 2
    wf.current_url = "https://example.com/"
    wf.snapshot = {}

    wf._record_rejected("输出里找不到 JSON 对象", None)

    assert len(wf.trace) == 1
    assert wf.trace[0]["ok"] is False
    assert "没有返回可解析" in wf.trace[0]["action"]


def test_nav_failure_records_step_zero():
    """起始页打不开时也要留一条，step=0 表示"还没做任何决策"。"""
    wf = make_workflow()
    wf._record_nav_failure("https://books.toscrape.com/", "TimeoutError: Timeout 20000ms exceeded")

    assert len(wf.trace) == 1
    row = wf.trace[0]
    assert row["step"] == 0
    assert row["url"] == "https://books.toscrape.com/"
    assert "Timeout" in row["error"]


# ---------------- plan / progress 的吸收 ----------------
def test_absorb_plan_keeps_first_plan():
    """plan 只认第一次：中途改计划会让已经打的勾全部失效，进度就废了。"""
    wf = make_workflow()
    wf._absorb_plan({"plan": ["点下一页", "读作者"]})
    wf._absorb_plan({"plan": ["完全不同的计划"]})
    assert wf.plan == ["点下一页", "读作者"]


def test_absorb_progress_drops_out_of_range():
    """越界序号必须丢掉。

    旧写法 limit = max(len(plan), max(numbers)) 会放行"9 号"挂在 3 条计划上，
    渲染时对应不到任何一行 —— 模型同时看到"还剩 3 条未完成"和 progress=[9]。
    """
    wf = make_workflow()
    wf._absorb_plan({"plan": ["a", "b", "c"]})
    wf._absorb_plan({"progress": [1, 9, "2"]})
    assert wf.progress == [1, 2]


def test_absorb_progress_empty_does_not_clear():
    """模型这轮漏了字段或给 [] 时，不能把已完成清空——那等于告诉它进度倒退了。"""
    wf = make_workflow()
    wf._absorb_plan({"plan": ["a", "b"]})
    wf._absorb_plan({"progress": [1, 2]})
    wf._absorb_plan({"progress": []})
    wf._absorb_plan({})
    assert wf.progress == [1, 2]


def test_absorb_progress_without_plan_is_ignored():
    """没有 plan 时 progress 无处挂靠，收了也渲染不出来。"""
    wf = make_workflow()
    wf._absorb_plan({"progress": [1, 2]})
    assert wf.plan == [] and wf.progress == []


# ---------------- 动作回显（P7-D1 T9 的回归） ----------------
# T9 实测原样：快照写着 `e1 link 上一页`，模型 thought 是"点击下一页按钮"，
# 照样点了 e1，一路往回翻，最后在第一页上答"下一页还能点"。
# 修法不是改题，是执行完之后把它实际点到的东西告诉它。
def test_target_label_uses_text_and_role():
    wf = make_workflow()
    wf.snapshot = {"elements": [{"id": "e1", "role": "link", "text": "上一页"}]}
    assert wf._target_label("e1") == "上一页（link）"


def test_target_label_falls_back_to_value_for_input():
    """输入框的可见文字经常是空的，回显成"（input）"对模型没有信息量。"""
    wf = make_workflow()
    wf.snapshot = {"elements": [{"id": "e1", "role": "input", "text": "", "value": "zzz"}]}
    assert wf._target_label("e1") == "zzz（input）"


def test_target_label_marks_empty_input_explicitly():
    wf = make_workflow()
    wf.snapshot = {"elements": [{"id": "e1", "role": "input", "text": "", "value": ""}]}
    assert "空输入框" in wf._target_label("e1")


def test_target_label_returns_empty_for_unknown_id():
    """编号对不上只说明状态变了，不该因此把任务判失败，所以返回空串让调用方跳过。"""
    wf = make_workflow()
    wf.snapshot = {"elements": [{"id": "e1", "role": "link", "text": "上一页"}]}
    assert wf._target_label("e9") == ""
    assert wf._target_label("") == ""


def test_target_label_without_snapshot_does_not_crash():
    wf = make_workflow()
    assert wf.snapshot is None
    assert wf._target_label("e1") == ""
