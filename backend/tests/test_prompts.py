"""提示词拼装层的单测：只测纯函数，不调模型。

为什么这几条必须测：`short_error` 是**唯一**能把浏览器报错变成模型能读的一句话的地方。
它错了不会报错，只会让模型"看懂了但理解反了"——最难发现的那类 bug。
"""
from __future__ import annotations

from app.agent.prompts import build_user_prompt, short_error

# 真实样本：MDN 首页点击「Skip to search」超时时的原始报错（已截短中间重复段）
REAL_CLICK_TIMEOUT = (
    "TimeoutError: Locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n"
    "  - waiting for locator(\"[data-wp-id=e24]\")\n"
    "    - locator resolved to <a data-wp-id=\"e24\" href=\"#mdn-search\">Skip to search</a>\n"
    "  - attempting click action\n"
    "    2 × waiting for element to be visible, enabled and stable\n"
    "      - element is visible, enabled and stable\n"
    "      - scrolling into view if needed\n"
    "      - done scrolling\n"
    "      - element is outside of the viewport\n"
    "    - retrying click action\n"
    "      - waiting 100ms\n"
    "    19 × waiting for element to be visible, enabled and stable\n"
    "      - element is outside of the viewport\n"
    " - retrying click action\n"
    "   - waiting 500ms\n"
)


def test_short_error_prefers_viewport_cause_over_timeout():
    """同一个报错里既有"视口外"也有"超时"，必须命中前者。

    两条信息的修法完全相反：
    - 超时 → "再等等"
    - 视口外 → "换元素或先滚动"
    判反了，模型会一直等一个永远点不到的元素。
    """
    result = short_error(REAL_CLICK_TIMEOUT)
    assert "视口外" in result
    assert "超时" not in result


def test_short_error_much_shorter_than_original():
    """核心目的：1065 字符的英文日志必须被压到一句话。"""
    result = short_error(REAL_CLICK_TIMEOUT)
    assert len(result) < 50
    assert len(result) < len(REAL_CLICK_TIMEOUT) / 10


def test_short_error_click_timeout_without_viewport_cause():
    err = 'TimeoutError: Locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting...'
    assert "点击超时" in short_error(err)


def test_short_error_keeps_network_error_code():
    """网络错误必须带出错误码，否则"网络错误"四个字等于没说。"""
    err = "Error: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid/"
    result = short_error(err)
    assert "ERR_NAME_NOT_RESOLVED" in result


def test_short_error_unknown_error_keeps_first_line_only():
    err = "SomeWeirdError: 出了点怪事\n第二行不该出现\n第三行也不该"
    result = short_error(err)
    assert "SomeWeirdError" in result
    assert "第二行" not in result


def test_short_error_empty():
    assert short_error("") == ""
    assert short_error(None) == ""  # type: ignore[arg-type]


# ---------------- 拼 prompt 时不塞长报错 ----------------
def test_history_error_is_compressed_in_prompt():
    """历史里的失败记录必须是压缩后的，不能把整段 Call log 拼进去。"""
    prompt = build_user_prompt(
        goal="测一下",
        step=2,
        max_steps=12,
        snapshot_text="[URL] http://x/\n[可交互元素] 共 0 个",
        history=[
            {"step": 1, "action": "click e24", "ok": False, "error": REAL_CLICK_TIMEOUT},
        ],
    )
    assert "视口外" in prompt
    assert "Call log" not in prompt
    assert len(prompt) < 1200  # 整段 prompt 不该因为一条报错而失控


# ---------------- 任务分解（plan / progress） ----------------
def test_prompt_marks_done_items():
    """plan 逐条打勾：这是模型判断"还剩哪条"的唯一依据。"""
    prompt = build_user_prompt(
        goal="翻页读作者",
        step=2,
        max_steps=12,
        snapshot_text="[正文] …",
        history=[],
        plan=["点下一页", "读第一条作者"],
        progress=[1],
    )
    assert "1. 点下一页（已完成）" in prompt
    assert "2. 读第一条作者（未完成）" in prompt


def test_prompt_counts_remaining():
    """必须把"还剩几条"数给它。

    真实站点 R3 实测：逐条打勾它看得懂，但不会自己去数——于是站在正确的页面上
    仍然写"需要点 Next 查看下一页"，多点一步就答错了。这一行是那个 bug 的解药。
    """
    prompt = build_user_prompt(
        goal="读作者", step=2, max_steps=12, snapshot_text="x",
        history=[], plan=["进标签页", "读第一条"], progress=[1],
    )
    assert "还剩 1 条未完成" in prompt


def test_prompt_all_done_tells_it_to_finish():
    """全部打完勾时不能只说"还剩 0 条"，要明确催它收尾。"""
    prompt = build_user_prompt(
        goal="读作者", step=3, max_steps=12, snapshot_text="x",
        history=[], plan=["进标签页", "读第一条"], progress=[1, 2],
    )
    assert "已全部完成" in prompt and "finish" in prompt


def test_prompt_without_plan_has_no_plan_block_after_step2():
    """一步就能做完的任务不该被反复催 plan——那会把它推向"先做点什么"。"""
    prompt = build_user_prompt(
        goal="读标题", step=5, max_steps=12, snapshot_text="x", history=[],
    )
    assert "任务分解" not in prompt


def test_prompt_nudges_plan_on_early_steps_only():
    early = build_user_prompt(goal="多步任务", step=1, max_steps=12, snapshot_text="x", history=[])
    assert "补上 plan" in early


def test_remaining_line_does_not_gate_finish():
    """「还剩 N 条」绝不能写成硬闸门。

    回归证据（真实站点 R1）：写成"全部完成后立刻 finish"时，模型把它当成闸门——
    第 1 步 thought 里已经写出正确答案，仍因为 plan 还剩一条而继续点进作者页，
    把一道一步题走成 6 步 loop。plan 只能是提醒，闸门永远是"正文里有没有答案"。
    """
    prompt = build_user_prompt(
        goal="读作者", step=2, max_steps=12, snapshot_text="x",
        history=[], plan=["翻页", "读作者"], progress=[1],
    )
    assert "不是必须走完的清单" in prompt
    assert "直接 finish" in prompt


# ---------------- 用户否决（P5a-4） ----------------
def test_rejection_section_is_absent_by_default():
    """不传 rejection 时**整段不渲染**。

    这是硬要求，不是优化：M5a 回放器拿历史 trace 重建 prompt 时不会传它
    （老条目根本没有这个键）。默认空就保证 P5a-4 之前所有回放问出去的问题
    一个字都没变——否则新老结果没法比，整个回放体系的价值归零。
    """
    kwargs = dict(
        goal="填表并提交",
        step=3,
        max_steps=12,
        snapshot_text="[正文] 资料填写",
        history=[],
    )
    assert "[用户否决]" not in build_user_prompt(**kwargs)
    # 显式传空串也一样（回放器就是这么传的：decision.get(...) or ""）
    assert "[用户否决]" not in build_user_prompt(rejection="", **kwargs)


def test_rejection_section_sits_right_after_goal():
    """"用户否决"必须排在任务紧后面、快照之前。

    实测教训（P5a-4）：只放进末尾的 notice 时，模型连续 5 轮照旧点提交——
    它每轮重读"任务"（目标里写着"点击提交按钮"），把末尾那句"观察"当成次要信息。
    弱模型对位置极敏感，这一段的位置就是它的权重。
    """
    prompt = build_user_prompt(
        goal="填表并提交",
        step=5,
        max_steps=12,
        snapshot_text="[正文] 资料填写",
        history=[],
        notice="页面已更新",
        plan=["填表", "提交"],
        progress=[1],
        rejection="你上一步想执行「click [e6]」，被用户拒绝了。用户的理由：不要提交。",
    )
    assert prompt.index("[用户否决]") < prompt.index("[进度]")
    assert prompt.index("[用户否决]") < prompt.index("资料填写")
    # 一次性提示仍然在末尾附近，两者互不取代
    assert "[观察]" in prompt
