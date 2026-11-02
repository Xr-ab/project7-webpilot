"""vision.py 的单元测试。

验收线（P6 计划 A2）：5 条测试绿 + 429 退避实测日志。
全部用假客户端，不打真模型——真模型的可用性由 scripts/r1_vision_probe.py 负责验证，
单测要的是"代码路径对不对"，不能依赖网络。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import vision


def _reply(text: str):
    """造一个和 openai 响应同形状的最小对象：只需要 choices[0].message.content。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class _FakeCompletions:
    """假的 chat.completions：按队列依次返回结果（异常也当结果抛）。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_client(monkeypatch):
    """替换 vision 的进程内单例，返回 (fake_completions, 安装函数)。"""

    def install(results):
        completions = _FakeCompletions(results)
        fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        monkeypatch.setattr(vision, "_client", fake)
        # 重试等待清零：单测不该真睡 3s + 8s
        monkeypatch.setattr(vision, "_RETRY_WAITS", (0, 0))
        return completions

    return install


async def test_describe_returns_text_and_sends_image_first(fake_client):
    """成功路径：原样返回模型文本，且消息里图在前、问题在后。

    图在前的顺序是实测结论（GLM 视觉系先看图再读文字更稳），
    这条断言把它钉住——改顺序会被测出来。
    """
    completions = fake_client([_reply("  按钮「导出报表」，数字 12 表示今日任务  ")])
    png = b"\x89PNG\r\n\x1a\nfake"

    text = await vision.describe(png)

    assert text == "按钮「导出报表」，数字 12 表示今日任务"  # 已 strip
    sent = completions.calls[0]["messages"][0]["content"]
    assert sent[0]["type"] == "image_url", "图必须排第一"
    assert sent[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert sent[1]["type"] == "text"


async def test_empty_model_reply_becomes_none(fake_client):
    """模型返回空串 → None（上层据此判断"没看到"，而不是拿到一句空话当结论）。"""
    fake_client([_reply("   ")])
    assert await vision.describe(b"x") is None


async def test_retries_on_429_then_succeeds(fake_client):
    """429 值得等：第一轮限流、第二轮成功，最终仍拿到文本（预检 #41 的教训照抄）。"""
    limited = Exception("rate limited")
    limited.status_code = 429  # 和 openai.RateLimitError 同形状
    completions = fake_client([limited, _reply("图里是折线图")])

    text = await vision.describe(b"x")

    assert text == "图里是折线图"
    assert len(completions.calls) == 2, "应该重试一次"


async def test_gives_up_after_all_retries_without_raising(fake_client):
    """三次都 429 → 返回 None，绝不抛异常（视觉是增强项，不能把任务带崩）。"""
    def limited():
        exc = Exception("rate limited")
        exc.status_code = 429
        return exc

    completions = fake_client([limited(), limited(), limited()])

    assert await vision.describe(b"x") is None
    assert len(completions.calls) == 3  # 首发 + 两次重试


async def test_bad_key_does_not_retry(fake_client):
    """401（key 错）等多久都不会变好——只发一次，直接降级。"""
    unauthorized = Exception("unauthorized")
    unauthorized.status_code = 401
    completions = fake_client([unauthorized])

    assert await vision.describe(b"x") is None
    assert len(completions.calls) == 1, "4xx 里的非 429 不该重试"


async def test_oversized_image_is_rejected_without_calling_model(fake_client):
    """超 5MB 的图直接放弃：发出去也是 400，还白占一次往返和上下文。"""
    completions = fake_client([])
    big = b"x" * (vision.MAX_IMAGE_BYTES + 1)

    assert await vision.describe(big) is None
    assert completions.calls == [], "超限的图不该发出去"


async def test_empty_image_is_rejected_without_calling_model(fake_client):
    completions = fake_client([])
    assert await vision.describe(b"") is None
    assert completions.calls == []


# ---------------------------------------------------------------------------
# A3：触发策略真值表 + notice 注入
#
# 这些测试不启浏览器、不连模型：直接构造 workflow 实例，只调策略/组装函数。
# 理由和 decision 校验的测试一样——策略是纯判断，把它泡在浏览器里测，
# 失败时分不清是策略错还是环境错。
# ---------------------------------------------------------------------------
from types import SimpleNamespace as _NS  # noqa: E402

from app.agent import workflow as workflow_mod  # noqa: E402
from app.agent.workflow import BrowserAgentWorkflow  # noqa: E402


def _vision_settings(**over) -> _NS:
    """一份只含视觉字段的假配置（monkeypatch 顶替 workflow 模块里的 settings）。"""
    base = dict(
        vision_enabled=True,
        vision_auto_fallback=True,
        vision_model="glm-4v-flash",
        vision_trigger_min_elements=3,
        vision_trigger_max_failures=2,
    )
    base.update(over)
    return _NS(**base)


def _make_wf(*, elements_total: int = 10, **over) -> BrowserAgentWorkflow:
    """绕过 __init__（它要真 session 和 llm），只装配策略用到的状态。"""
    wf = BrowserAgentWorkflow.__new__(BrowserAgentWorkflow)
    wf.vision_forced = over.get("vision_forced", False)
    wf.consecutive_failures = over.get("consecutive_failures", 0)
    wf.vision_calls = over.get("vision_calls", 0)
    wf.snapshot = {"elements_total": elements_total}
    wf.session = _NS(page=object())
    return wf


async def test_trigger_master_switch_off_means_never(monkeypatch):
    """总闸关着 → 任何条件都不看（包括任务级强制）。

    这是回归防护的核心：视觉会改 prompt，总闸必须是"一票否决"。
    """
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings(vision_enabled=False))
    wf = _make_wf(elements_total=0, vision_forced=True, consecutive_failures=9)
    assert await wf._vision_trigger() is None


async def test_trigger_task_level_force(monkeypatch):
    """任务级 vision=true → 无条件看（canvas 演示/评测对比就靠它）。"""
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=50, vision_forced=True)
    assert await wf._vision_trigger() == "task"


async def test_trigger_consecutive_failures(monkeypatch):
    """连续 2 步失败 → 看（模型在猜，DOM 通道不够用了）。"""
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=50, consecutive_failures=2)
    assert await wf._vision_trigger() == "failures"


async def test_trigger_sparse_dom_with_canvas(monkeypatch):
    """DOM 稀疏 + 页面真有 canvas → 看（唯一站得住的"视觉能救"场景）。"""
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=1)

    async def fake_cues(page):
        return {"canvas": 1, "big_image": 0}

    monkeypatch.setattr(workflow_mod, "page_visual_cues", fake_cues)
    assert await wf._vision_trigger() == "sparse_dom"


async def test_trigger_sparse_dom_alone_is_not_enough(monkeypatch):
    """只有 DOM 稀疏、没有任何视觉线索 → 不看。

    纯文章页元素也少，但正文够模型读；看一眼截图纯属浪费。
    这条防的是"条件 1 被写成 elements_total < N 就触发"的退化——
    那样几乎每个文章页都会调一次视觉，成本和延迟都不可控。
    """
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=1)

    async def fake_cues(page):
        return {"canvas": 0, "big_image": 0}

    monkeypatch.setattr(workflow_mod, "page_visual_cues", fake_cues)
    assert await wf._vision_trigger() is None


async def test_trigger_respects_auto_fallback_switch(monkeypatch):
    """自动兜底关掉时，只有任务级强制能触发（压测/回归时的干净模式）。"""
    monkeypatch.setattr(
        workflow_mod, "settings", _vision_settings(vision_auto_fallback=False)
    )
    wf = _make_wf(elements_total=0, consecutive_failures=5)
    assert await wf._vision_trigger() is None

    # 同一个实例打开任务级强制：立刻能触发——两个开关互不牵连
    wf.vision_forced = True
    assert await wf._vision_trigger() == "task"


async def test_trigger_stops_after_call_budget(monkeypatch):
    """一个任务看满 2 次就不再看了（免费额度 + 每步几秒延迟都烧不起）。"""
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=50, vision_forced=True, vision_calls=2)
    assert await wf._vision_trigger() is None


async def test_vision_understand_returns_record_and_notice(monkeypatch):
    """成功时：trace 记录含 used/model/trigger/ms/summary，notice 明确标"仅供参考"。"""
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=50, vision_forced=True)

    async def fake_shot(**kwargs):
        return b"png-bytes"

    wf.session = _NS(page=_NS(screenshot=fake_shot))

    async def fake_describe(image, question=None, *, model=None):
        return "折线图显示 6 月销量最高，为 4200"

    monkeypatch.setattr(workflow_mod.vision, "describe", fake_describe)

    record, notice = await wf._vision_understand()

    assert record["used"] is True
    assert record["trigger"] == "task"
    assert record["model"] == "glm-4v-flash"
    assert "4200" in record["summary"]
    assert isinstance(record["ms"], int)
    # 措辞纪律：必须写明"参考"且"动作仍用元素编号"，否则模型会去点 canvas
    assert "视觉参考" in notice and "元素编号" in notice
    assert wf.vision_calls == 1


async def test_vision_understand_failure_keeps_record_but_no_notice(monkeypatch):
    """模型失败（限流/超时）→ 记录用 used=False 留痕，prompt 里一个字都不加。

    为什么不能塞一句"没看到"进去：模型会顺着这句话编故事（"可能是加载中"），
    比什么都不说更糟。
    """
    monkeypatch.setattr(workflow_mod, "settings", _vision_settings())
    wf = _make_wf(elements_total=50, vision_forced=True)

    async def fake_shot(**kwargs):
        return b"png-bytes"

    wf.session = _NS(page=_NS(screenshot=fake_shot))

    async def fake_describe(image, question=None, *, model=None):
        return None

    monkeypatch.setattr(workflow_mod.vision, "describe", fake_describe)

    record, notice = await wf._vision_understand()

    assert record["used"] is False
    assert "summary" not in record
    assert notice == ""
