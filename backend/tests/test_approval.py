"""P5a 审批停等测试（不需要浏览器、不需要数据库）。

分五层：
1. 风险判定规则（approval.gate）——纯函数，最容易穷举
2. 状态分组（app/status.py）——加新状态时最容易错的地方
3. 闸门本身（step_gate）——真的会返回 StopEvent(waiting_approval)，而不是放行
4. 审批项落库与隔离（approvals 表）——要真 PostgreSQL，没起库自动跳过
5. 续跑（P5a-3）——用假 session 验证"恢复现场 + 执行被批准的动作"

为什么第 3/5 层能脱离浏览器测：
step_gate 只读 self.snapshot / self.current_url / self.step_no 这几个纯数据字段；
_resume_start 只调 session.goto / click 这类方法。用一个记录调用的假 session
就能把"它到底做了什么"断言出来——这正是"判定与执行分离""workflow 只依赖
session 接口"带来的可测性。真浏览器只在 check_approval.py 的端到端里出现。
"""
from __future__ import annotations

import socket
from dataclasses import replace

import pytest
from llama_index.core.workflow import StartEvent, StopEvent
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine.url import make_url

from app.agent import approval
from app.agent.actions import Action, ActionType
from app.agent.workflow import ActEvent, BrowserAgentWorkflow, GateEvent, ObserveEvent
from app.config import settings
from app.db import repo
from app.db.models import Approval, Task, User
from app.db.session import SessionLocal, engine
from app.status import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    NEEDS_WORKER,
    PAUSED,
    UNFINISHED,
)
from app.security import vault


class _FakeLLM:
    """step_gate 不调模型，但构造 workflow 需要一个 llm。"""

    last_usage: dict = {}


_SNAPSHOT = {
    "url": "http://127.0.0.1:8000/testsite/form.html",
    "title": "资料填写",
    "text": "资料填写",
    "elements": [
        {"id": "e1", "role": "input", "text": "姓名", "input_type": "text"},
        {"id": "e2", "role": "input", "text": "密码", "input_type": "password"},
        {"id": "e3", "role": "button", "text": "提交"},
        {"id": "e4", "role": "link", "text": "下一页"},
        {"id": "e5", "role": "button", "text": ""},
        {"id": "e6", "role": "input", "text": "电话", "input_type": "tel"},
    ],
    "elements_total": 6,
    "elements_shown": 6,
    "modal_open": False,
}


def _click(target: str) -> Action:
    return Action(thought="", action=ActionType.CLICK, target=target)


def _type(target: str, text: str = "x") -> Action:
    return Action(thought="", action=ActionType.TYPE, target=target, text=text)


# ---------------------------------------------------------------- 1. 风险规则


def test_click_submit_is_blocked():
    reason = approval.gate(_click("e3"), _SNAPSHOT)
    assert reason and "提交" in reason


def test_click_plain_link_is_allowed():
    assert approval.gate(_click("e4"), _SNAPSHOT) is None


def test_type_password_is_blocked():
    reason = approval.gate(_type("e2"), _SNAPSHOT)
    assert reason and "密码" in reason


def test_type_plain_field_is_allowed():
    assert approval.gate(_type("e1"), _SNAPSHOT) is None
    assert approval.gate(_type("e6"), _SNAPSHOT) is None


def test_click_without_label_is_allowed():
    """纯图标按钮没有文字，无法判定风险 —— 放行而不是一律拦。

    理由：拦下来也没用，用户看到"未知元素"没法做决策，只会在审批上白点一次。
    """
    assert approval.gate(_click("e5"), _SNAPSHOT) is None


def test_unknown_target_is_allowed():
    """模型引用了不存在的编号：动作层自己会报错，闸门不必抢着报。"""
    assert approval.gate(_click("e99"), _SNAPSHOT) is None
    assert approval.gate(_click("e99"), None) is None


@pytest.mark.parametrize(
    "action_type",
    [ActionType.GOTO, ActionType.SCROLL, ActionType.SELECT],
)
def test_non_click_actions_are_allowed(action_type):
    """goto 有域名白名单、scroll 无副作用、select 在测试站无风险。

    都不该走审批——否则一个 10 步任务要点 10 次批准，闸门会被用户当噪音忽略。
    """
    act = Action(thought="", action=action_type, target="e1", url="http://127.0.0.1/", option="x")
    assert approval.gate(act, _SNAPSHOT) is None


def test_finish_is_allowed():
    act = Action(thought="", action=ActionType.FINISH, answer="42")
    assert approval.gate(act, _SNAPSHOT) is None


# ---------------------------------------------------------------- 2. 状态分组


def test_waiting_approval_is_not_needs_worker():
    """"待审批"不该被 worker 推进，也不该被启动补偿当中断清掉。

    mark_interrupted 只扫 NEEDS_WORKER；如果误把待审批放进去，
    用户去泡杯茶回来，任务就变成"进程重启，已中断"了。
    """
    assert "waiting_approval" not in NEEDS_WORKER
    assert "waiting_approval" in PAUSED


def test_waiting_approval_counts_as_unfinished():
    """统计口径：待审批不能进成功率分母。

    否则成功率 = success / (success + error + … + 待审批)，
    一批还没跑完的任务会把成功率越拉越低 —— 数字失真比没数字更糟。
    """
    assert "waiting_approval" in UNFINISHED
    assert "queued" in UNFINISHED and "running" in UNFINISHED
    assert "success" not in UNFINISHED


def test_unfinished_is_composed_of_two_groups():
    assert set(UNFINISHED) == set(NEEDS_WORKER) | set(PAUSED)


# ---------------------------------------------------------------- 3. 闸门本体


def _workflow(**kwargs) -> BrowserAgentWorkflow:
    wf = BrowserAgentWorkflow(session=None, llm=_FakeLLM(), **kwargs)  # type: ignore[arg-type]
    wf.goal = "填写表单并提交"
    wf.step_no = 4
    wf.snapshot = _SNAPSHOT  # type: ignore[assignment]
    wf.current_url = _SNAPSHOT["url"]
    return wf


async def test_gate_stops_on_risky_action():
    wf = _workflow()
    out = await wf.step_gate(None, GateEvent(action=_click("e3")))  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    assert out.result["status"] == "waiting_approval"
    # 暂停不是失败：不能带"失败"语义的字段
    assert out.result["note"]
    # 恢复续跑要的上下文必须齐全，少一个都得重开浏览器后瞎猜
    ap = out.result["approval"]
    assert ap["step_no"] == 4
    assert ap["snapshot_url"] == _SNAPSHOT["url"]
    assert ap["action"]["action"] == "click"
    assert ap["action"]["target"] == "e3"
    assert ap["context"]["goal"] == "填写表单并提交"
    assert ap["context"]["current_url"] == _SNAPSHOT["url"]


async def test_gate_passes_safe_action():
    wf = _workflow()
    out = await wf.step_gate(None, GateEvent(action=_click("e4")))  # type: ignore[arg-type]
    assert isinstance(out, ActEvent)
    assert out.action.target == "e4"


async def test_gate_is_skipped_when_task_opts_out():
    """任务级开关：批处理脚本提交 require_approval=false 时一律放行。"""
    wf = _workflow(approval_enabled=False)
    out = await wf.step_gate(None, GateEvent(action=_click("e3")))  # type: ignore[arg-type]
    assert isinstance(out, ActEvent)


async def test_gate_respects_global_switch(monkeypatch):
    """进程级开关是总闸：即使任务没 opt-out，全局关掉也必须放行（紧急止血）。"""
    # 用 dataclasses.replace 从**真配置**复制一份、只翻转一个字段，
    # 而不是手写一个假配置类。理由有两个：
    # 1. Settings 是 frozen dataclass，setattr 会抛 FrozenInstanceError，改不了原件；
    # 2. 假类只声明我当下想到的字段，workflow.__init__ 以后多读一个配置
    #    （这次就漏了 max_steps / task_timeout_seconds）测试立刻 AttributeError 崩掉，
    #    而 replace 出来的对象字段天然齐全，加多少配置都不用跟着改测试。
    monkeypatch.setattr(
        "app.agent.workflow.settings",
        replace(settings, approval_enabled=False),
    )
    wf = _workflow()
    out = await wf.step_gate(None, GateEvent(action=_click("e3")))  # type: ignore[arg-type]
    assert isinstance(out, ActEvent)


async def test_gate_records_trace_and_calls_callback():
    """暂停也要留痕，并且必须回调落库——否则重启后没人知道在等什么。"""
    captured: list[dict] = []

    async def on_approval(pending: dict) -> None:
        captured.append(pending)

    wf = _workflow(on_approval=on_approval)
    await wf.step_gate(None, GateEvent(action=_click("e3")))  # type: ignore[arg-type]

    assert len(captured) == 1
    assert captured[0]["reason"]
    # 轨迹里要有一条 pending_approval 标记，前端据此标出"这一步没真执行"
    assert wf.trace[-1].get("pending_approval") is True
    # 而且必须走标准 _record 的字段形状，否则回放器读到 None 会崩
    for key in ("step", "url", "action", "decision", "llm_usage"):
        assert key in wf.trace[-1]


# ------------------------------------------------------- 3.5 密码脱敏（P5b-②）

# 挑一个不像测试词表里的假密码：断言"整个 trace 序列化后不含它"才有说服力
_SECRET = "s3cret-9x7!"


def _trace_blob(wf: BrowserAgentWorkflow) -> str:
    """把整个轨迹（含嵌套 decision）压成一个字符串，用来断言"哪儿都没有明文"。"""
    import json as _json

    return _json.dumps(wf.trace, ensure_ascii=False)


async def test_password_type_redacts_trace_and_pending():
    """闸门拦下"往密码框输入"：轨迹、决策上下文、待审批信息里都不许有明文。

    三个落点各验各的：
    - trace 行的 action / thought：前端时间线和实时 prompt 都从这里取
    - decision.payload.text：模型原始输出，回放器读它重建"当时这一问"
    - pending_approval：接下来要进 approvals 表和 meta.json 的那份
      （text 打码成 ***，真值加密进 _secret——批准续跑靠它还原）
    """
    wf = _workflow()
    # 真实流程里 step_decide 已经把模型原始输出冻结进 _last_payload（含明文）；
    # 测试没有 decide 这一步，手动补上，才能验证 decision 里那份也被洗掉
    wf._last_payload = {
        "thought": f"我要在密码框输入 {_SECRET}",
        "action": "type",
        "target": "e2",
        "text": _SECRET,
    }
    action = Action(
        thought=f"我要在密码框输入 {_SECRET}",
        action=ActionType.TYPE,
        target="e2",
        text=_SECRET,
    )
    out = await wf.step_gate(None, GateEvent(action=action))  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    blob = _trace_blob(wf)
    assert _SECRET not in blob
    assert "***" in blob
    # 具体到三个位置（上面的整体断言万一 *** 覆盖了别的字段也别糊弄过去）
    assert wf.trace[-1]["action"] == "type e2 <- '***'"
    assert wf.trace[-1]["decision"]["payload"]["text"] == "***"

    ap = out.result["approval"]
    assert ap["action"]["text"] == "***"
    # 真值以密文随行：能解回来，且密文本身不含明文
    assert ap["action"]["_secret"] != _SECRET
    assert vault.decrypt_text(ap["action"]["_secret"]) == _SECRET


async def test_rejected_password_payload_is_redacted():
    """被拒路径（动作没 parse 成功/被判非法）里模型原始输出也带密码，同样要洗。"""
    wf = _workflow()
    payload = {
        "thought": f"填写 {_SECRET} 到密码框",
        "action": "type",
        "target": "e2",
        "text": _SECRET,
    }
    wf._record_rejected("e2 是 button，不能输入文字", payload)

    blob = _trace_blob(wf)
    assert _SECRET not in blob
    assert wf.trace[-1]["thought"] == f"填写 *** 到密码框"


async def test_resume_approved_password_restores_secret(monkeypatch):
    """批准续跑：库里的 text 是 ***，真值必须从 _secret 解密还原后再执行。

    这是"加密随行"存在的唯一理由——还原不出来宁可失败，
    绝不能把字面 *** 填进密码框（动作成功但做的是错事）。
    """
    _patch_snapshot(
        monkeypatch,
        elements=[{"id": "e2", "role": "input", "text": "密码", "input_type": "password"}],
    )
    session = _FakeSession()
    wf = _resume_workflow(
        session,
        action={
            "thought": "填密码",
            "action": "type",
            "target": "e2",
            "text": "***",
            "_secret": vault.encrypt_text(_SECRET),
        },
    )

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    # 页面上收到的是**真值**，不是 *** 字面量
    assert any(k == "type" and _SECRET in v for k, v in session.calls)
    assert not any(k == "type" and "***" in v for k, v in session.calls)
    # 这一趟的 trace 行也不落明文
    assert _SECRET not in _trace_blob(wf)
    # 兜底清洗源用完即清：不影响后续步骤的正常记录
    assert wf._resume_secret is None


# ---------------------------------------------------------------- 4. 落库与隔离（P5a-2）


def _db_reachable() -> bool:
    url = make_url(settings.database_url)
    try:
        with socket.create_connection((url.host or "127.0.0.1", url.port or 5432), timeout=0.5):
            return True
    except OSError:
        return False


# 没起库时跳过而不是失败：这一层验的是 SQL 写对没有，
# 纯逻辑那三层在没数据库的机器上照样要能全绿
db_required = pytest.mark.skipif(
    not _db_reachable(), reason="PostgreSQL 未启动；用 docker 起 wp-postgres 容器后再跑"
)


def test_from_meta_fills_missing_keys():
    """meta.json 里那份是 workflow 直出的五键，API 形状要有 id / created_at / waiting_s。

    补空值而不是"让前端判两套形状"：前端只该认一种返回结构。
    """
    from app.api.approvals import _from_meta

    raw = {
        "step_no": 4,
        "reason": "点击「提交」属于高风险动作",
        "action": {"action": "click", "target": "e3"},
        "snapshot_url": "http://127.0.0.1/testsite/form.html",
        "context": {"goal": "填表", "plan": [], "progress": [], "step_no": 4},
    }
    out = _from_meta(raw)
    # raw 里没有 id（表写失败了）→ 透传出来就是 None。
    # 表写成功时 on_approval 会把 id 塞进 meta，那时原样透传，不能丢。
    assert out["id"] is None
    assert out["created_at"] is None    # meta 没记时间，宁可为空也不编一个
    assert out["waiting_s"] is None
    assert out["status"] == "pending"
    # 业务字段必须原样透传，少一个续跑就瞎猜
    assert out["step_no"] == 4
    assert out["action"]["target"] == "e3"
    assert out["context"]["goal"] == "填表"


_PREFIX = "approval-test-"


async def test_route_ignores_stale_meta_when_task_finished(monkeypatch):
    """任务已经跑完时，即使 meta 里还留着 approval，也不能返回"待审批"。

    这是个真实的幻影 bug 源：meta.json 里的 approval 是"暂停那一刻"的快照，
    批准之后没人清它。不加状态门，接口会一直返回 pending，
    前端于是永远挂着一张审批卡——而那个动作其实早就执行过了。
    """
    from app.api import approvals as api_mod

    async def fake_get_task(_task_id):
        return {
            "status": "success",          # 已经跑完
            "user_id": 1,
            "approval": {"step_no": 4, "reason": "旧快照", "action": {}, "context": {}},
        }

    async def no_pending(_task_id, user_id=None):
        return None

    monkeypatch.setattr(api_mod.task_runner, "get_task", fake_get_task)
    monkeypatch.setattr(api_mod.repo, "fetch_pending_approval", no_pending)

    # user=None → assert_owned 放行（本地模式），这条测的是状态门、不是隔离
    out = await api_mod.get_approval("t-whatever", None)
    assert out["pending"] is None
    assert out["status"] == "success"


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """清空连接池（同 test_stats / test_isolation）。

    引擎连接池是进程级单例，pytest-asyncio 每个测试新建事件循环；
    上个测试留下的连接绑在旧循环上，复用会抛 "Event loop is closed"。
    """
    await engine.dispose()
    yield


@pytest.fixture
async def seeded():
    """两个用户 + A 的一个任务；测试结束按前缀清干净，不污染真库。"""
    async with SessionLocal() as session:
        alice = User(email=f"{_PREFIX}alice@example.com", password_hash="x")
        bob = User(email=f"{_PREFIX}bob@example.com", password_hash="x")
        session.add_all([alice, bob])
        await session.commit()
        await session.refresh(alice)
        await session.refresh(bob)
        ids = {"alice": alice.id, "bob": bob.id, "task": f"{_PREFIX}t1"}

    await repo.create_task(
        task_id=ids["task"],
        goal="填写表单并提交",
        start_url="http://127.0.0.1:8000/testsite/form.html",
        model="test",
        user_id=ids["alice"],
    )
    try:
        yield ids
    finally:
        # 先删子表再删父表：有 CASCADE 也能直接删 tasks，
        # 但显式按序删让清理逻辑不依赖外键定义（将来有人去掉 CASCADE 也不会留脏数据）
        async with SessionLocal() as session:
            await session.execute(delete(Approval).where(Approval.task_id.like(f"{_PREFIX}%")))
            await session.execute(delete(Task).where(Task.id.like(f"{_PREFIX}%")))
            await session.execute(delete(User).where(User.email.like(f"{_PREFIX}%")))
            await session.commit()


@db_required
async def test_create_and_fetch_pending_approval(seeded):
    """核心链路：闸门停下写一条 pending → 查得回来，字段一个不少。"""
    approval_id = await repo.create_approval(
        task_id=seeded["task"],
        step_no=4,
        action={"action": "click", "target": "e3", "thought": "点提交"},
        reason="点击「提交」属于高风险动作（命中关键词：提交）",
        snapshot_url="http://127.0.0.1:8000/testsite/form.html",
        context={"goal": "填写表单并提交", "plan": ["填姓名"], "progress": [1], "step_no": 4},
    )
    assert approval_id is not None

    pending = await repo.fetch_pending_approval(seeded["task"])
    assert pending is not None
    assert pending["id"] == approval_id
    assert pending["step_no"] == 4
    assert pending["status"] == "pending"
    # 恢复时要靠它重新 goto：浏览器早就关了，没有这一步就不知道回哪个页面
    assert pending["snapshot_url"].endswith("form.html")
    # 恢复时要靠它接上模型的思路（goal / plan / progress）
    assert pending["context"]["progress"] == [1]
    assert pending["action"]["target"] == "e3"
    # waiting_s 用来显示"已等待 X 分钟"；刚创建应该是 0，不能是负数
    assert pending["waiting_s"] is not None and pending["waiting_s"] >= 0


@db_required
async def test_pending_approval_is_owner_filtered(seeded):
    """B 查 A 的待审批 → None（上层据此回 404，与查 A 的任务详情表现一致）。"""
    await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url="http://127.0.0.1/", context={},
    )
    assert await repo.fetch_pending_approval(seeded["task"], user_id=seeded["bob"]) is None
    # 不加过滤（worker / 本地模式）才看得到
    assert await repo.fetch_pending_approval(seeded["task"], user_id=seeded["alice"]) is not None


@db_required
async def test_resolved_approval_is_not_pending(seeded):
    """批过/拒过之后，同一任务不该再被当成"还有待审批"。

    这条防止一个经典错位：前端卡片在用户点完批准后还挂着，
    再点一次又发一次 POST，任务被续跑两次。
    """
    approval_id = await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url="http://127.0.0.1/", context={},
    )
    async with SessionLocal() as session:
        await session.execute(
            update(Approval).where(Approval.id == approval_id).values(status=APPROVAL_APPROVED)
        )
        await session.commit()

    assert await repo.fetch_pending_approval(seeded["task"]) is None


@db_required
async def test_multiple_approvals_returns_latest_pending(seeded):
    """一个任务多次审批是正常的（第 4 步提交、第 7 步删除）。

    同时存在两条 pending 属于异常数据（进程在两次审批之间被 kill），
    这时要返回**最新那条**——报错只会让任务永远卡死，没有可操作性。
    """
    for step in (4, 7):
        await repo.create_approval(
            task_id=seeded["task"], step_no=step, action={"action": "click", "target": f"e{step}"},
            reason=f"第 {step} 步高风险", snapshot_url="http://127.0.0.1/", context={},
        )
    pending = await repo.fetch_pending_approval(seeded["task"])
    assert pending["step_no"] == 7


@db_required
async def test_deleting_task_cascades_approvals(seeded):
    """任务删了，审批记录必须跟着走（否则留下点不开的孤儿审计行）。"""
    await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url="http://127.0.0.1/", context={},
    )
    async with SessionLocal() as session:
        await session.execute(delete(Task).where(Task.id == seeded["task"]))
        await session.commit()
        left = await session.scalar(
            select(func.count()).select_from(Approval).where(Approval.task_id == seeded["task"])
        )
    assert left == 0


# ---------------------------------------------------------------- 5. 续跑（P5a-3）


class _FakeSession:
    """只记录"被要求做了什么"的假浏览器。

    为什么不用 Playwright：这一层要验的是"续跑时 workflow 有没有回到正确地址、
    有没有执行被批准的那个动作、trace 有没有接上"——全是**决策逻辑**，
    跟 Chromium 没有关系。真浏览器留给 check_approval.py 的端到端跑一次就够，
    放进单测会让每条约 3 秒变成整片红的时候分不清是逻辑错还是环境错。
    """

    def __init__(self, fail_click: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_click = fail_click
        # _resume_start 要 take_snapshot(self.session.page)，这里给个占位对象
        self.page = object()

    async def goto(self, url: str) -> None:
        self.calls.append(("goto", url))

    async def click(self, selector: str) -> None:
        if self.fail_click:
            raise RuntimeError("element detached")
        self.calls.append(("click", selector))

    async def type_text(self, selector: str, text: str) -> None:
        self.calls.append(("type", f"{selector}={text}"))

    async def select_option(self, selector: str, option: str) -> None:
        self.calls.append(("select", f"{selector}={option}"))

    async def scroll(self, direction: str) -> None:
        self.calls.append(("scroll", direction))

    async def screenshot_to(self, path) -> None:
        self.calls.append(("screenshot", str(path)))


_PAUSE_URL = "http://127.0.0.1:8000/testsite/form.html"

# 暂停时的快照里，提交按钮是 e3
_OLD_ELEMENTS = [
    {"id": "e1", "role": "input", "text": "姓名"},
    {"id": "e3", "role": "button", "text": "提交"},
]


def _make_snapshot(elements: list[dict]) -> dict:
    """造一份字段齐全的 PageSnapshot。

    必须齐全（含 scroll / text_truncated）：_resume_start 里会调
    render_snapshot_text，缺键直接 KeyError。这也是"用真渲染函数"的代价，
    而这个代价值得付——假渲染会让"续跑后模型看到什么"这件事测不准。
    """
    return {
        "url": _PAUSE_URL,
        "title": "资料填写",
        "headings": [],
        "text": "资料填写",
        "text_truncated": False,
        "elements": elements,
        "elements_total": len(elements),
        "elements_shown": len(elements),
        "modal_open": False,
        "scroll": {"y": 0, "page_height": 900, "viewport": 800},
    }


def _patch_snapshot(monkeypatch, elements: list[dict] | None = None) -> None:
    """把续跑时拍到的"新页面"替换成给定元素清单。

    默认让提交按钮的编号**变掉**（e3 → e5）：真实场景里重开浏览器后编号很可能不同，
    而"按旧编号硬点"正是 P5a-3 实测踩到的那个坑（点了但什么也没发生）。
    """
    snapshot = _make_snapshot(
        elements
        if elements is not None
        else [
            {"id": "e2", "role": "input", "text": "姓名"},
            {"id": "e5", "role": "button", "text": "提交"},
        ]
    )

    async def fake_snapshot(_page):
        return snapshot

    monkeypatch.setattr("app.agent.workflow.take_snapshot", fake_snapshot)


def _paused_trace() -> list[dict]:
    """暂停时磁盘上已经有的 4 行轨迹（最后一行的 decision 会被续跑复用）。"""
    rows = []
    for step in (1, 2, 3):
        rows.append(
            {
                "step": step,
                "url": _PAUSE_URL,
                "thought": f"第 {step} 步的想法",
                "action": f"type [{step}]",
                "ok": True,
                "error": None,
                "duration_ms": 100,
                "elements_total": 2,
                "screenshot": f"step_{step}.png",
                "llm_usage": {},
                "decision": {"snapshot_text": f"快照{step}", "snapshot": None, "payload": None},
            }
        )
    # 第 4 步：被闸门停下的那一行（有 pending_approval 标记，没有真的执行）
    rows.append(
        {
            "step": 4,
            "url": _PAUSE_URL,
            "thought": "点击提交按钮",
            "action": "click [e3]",
            "ok": True,
            "error": None,
            "duration_ms": 0,
            "elements_total": 2,
            "screenshot": None,
            "llm_usage": {},
            "pending_approval": True,
            "decision": {
                "snapshot_text": "第 4 步的快照正文",
                # 这一份是**重新定位元素的唯一依据**：续跑靠它把 e3 翻译成新编号
                "snapshot": _make_snapshot(_OLD_ELEMENTS),
                "payload": {"action": "click", "target": "e3"},
                "plan": ["填姓名"],
                "progress": [1],
                "notice": "",
                "error_hint": "",
                "max_steps": 12,
            },
        }
    )
    return rows


def _resume_state(**overrides) -> dict:
    state = {
        "action": {"thought": "点击提交按钮", "action": "click", "target": "e3"},
        "snapshot_url": _PAUSE_URL,
        "context": {
            "goal": "填写表单并提交",
            "plan": ["填姓名", "提交"],
            "progress": [1],
            "current_url": _PAUSE_URL,
            "step_no": 4,
        },
        "trace": _paused_trace(),
    }
    state.update(overrides)
    return state


def _resume_workflow(session: _FakeSession | None = None, **kw) -> BrowserAgentWorkflow:
    return BrowserAgentWorkflow(
        session=session or _FakeSession(),  # type: ignore[arg-type]
        llm=_FakeLLM(),
        resume=_resume_state(**kw),
    )


async def test_resume_restores_state_and_executes_approved_action(monkeypatch):
    """续跑必须做到四件事：回现场、执行被批准的动作、接上步号、接上 trace。"""
    _patch_snapshot(monkeypatch)
    session = _FakeSession()
    wf = _resume_workflow(session)

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    # 1. 回到暂停时的页面（不是 start_url、也不是从头开始）
    assert session.calls[0] == ("goto", _PAUSE_URL)
    # 2. 执行了**被批准的那个动作**，而不是重新问模型
    assert session.calls[1][0] == "click"
    assert wf.llm.last_usage == {}  # 没调模型（假 llm 只会被 decide 调用）
    # 3. 步号接上：下一步 observe 会 +1，所以这里应该停在 4
    assert wf.step_no == 4
    # 4. trace 接上：4 条旧行 + 1 条新执行行
    assert len(wf.trace) == 5
    assert wf.trace[0]["step"] == 1  # 前 N 行原样保留
    assert wf.trace[3]["pending_approval"] is True  # 暂停那一行还在
    assert wf.trace[-1]["step"] == 4
    assert wf.trace[-1]["approved"] is True  # 审计标记
    assert wf.trace[-1]["ok"] is True
    # 恢复出来的上下文
    assert wf.goal == "填写表单并提交"
    assert wf.plan == ["填姓名", "提交"]
    assert wf.progress == [1]
    # 还没结束，进循环
    assert not isinstance(out, StopEvent)


async def test_resume_relocates_element_by_role_and_text(monkeypatch):
    """编号变了也要点得到：靠「button 提交」重新定位，而不是硬用旧编号 e3。

    这条是整个续跑里最容易漏、后果最隐蔽的一环。旧编号 e3 对应的是
    `[data-wp-id="e3"]` 这个 DOM 属性，而浏览器是**新开的**，页面上没有这个属性。
    按旧编号直接点 → 动作失败但任务继续跑，看起来像"审批没生效"。
    """
    # 新页面里提交按钮的编号从 e3 变成了 e5
    _patch_snapshot(monkeypatch)
    session = _FakeSession()
    wf = _resume_workflow(session)

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    selector = session.calls[1][1]
    assert "e5" in selector, f"应该用重新定位后的编号，实际点的是 {selector}"
    assert "e3" not in selector


async def test_resume_skips_action_when_element_gone(monkeypatch):
    """恢复后那个元素彻底不见了 → **不执行**，并告诉模型页面变了。

    不执行是刻意的：人批准的是"提交那个按钮"，不是"随便找个像的点点"。
    点了别的元素可能造成谁也没预料到的副作用，比什么都不做严重得多。
    """
    # 新页面里没有「提交」按钮了
    _patch_snapshot(monkeypatch, elements=[{"id": "e1", "role": "link", "text": "返回首页"}])
    session = _FakeSession()
    wf = _resume_workflow(session)

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    # 只 goto，没点任何东西
    assert [c[0] for c in session.calls] == ["goto"]
    assert not isinstance(out, StopEvent)  # 不是失败，是"让模型重新判断"
    last = wf.trace[-1]
    assert last["ok"] is False
    assert last["resume_skipped"] is True
    assert "找不到" in (last["error"] or "")
    # 这条说明必须传给模型，否则它以为上一步成功了
    assert "没有执行" in wf.resume_notice


async def test_resume_notice_reaches_the_next_prompt(monkeypatch):
    """跳过/失败的说明要在下一轮 observe 里交给模型，而且只说一次。"""
    _patch_snapshot(monkeypatch, elements=[{"id": "e1", "role": "link", "text": "返回首页"}])
    wf = _resume_workflow()
    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    ev = await wf.step_observe(None, ObserveEvent())  # type: ignore[arg-type]

    assert "没有执行" in ev.notice
    # 一次性信息：第二轮不该再重复（重复会让模型以为又出事了）
    assert wf.resume_notice == ""


async def test_resume_without_url_fails_honestly():
    """没有暂停页地址 → 直接失败，**不能**偷偷退回从头重跑。

    从头重跑等于绕过审批：没人批过的动作也可能被执行一遍。
    """
    session = _FakeSession()
    wf = _resume_workflow(session, snapshot_url="", context={"goal": "x", "step_no": 4})

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    assert out.result["status"] == "bad_request"
    assert "暂停页" in out.result["note"]
    assert session.calls == []  # 一个动作都没执行


async def test_resume_action_failure_does_not_kill_task(monkeypatch):
    """批准的动作执行失败（元素脱挂）不算任务失败，照常进循环。

    恢复后页面已变是**已知代价**（浏览器是新开的）。这时候正确做法是
    让模型看到 ok=False 这一行，自己想办法——而不是把任务判死。
    """
    _patch_snapshot(monkeypatch)
    session = _FakeSession(fail_click=True)
    wf = _resume_workflow(session)

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert not isinstance(out, StopEvent)
    last = wf.trace[-1]
    assert last["ok"] is False
    assert "element detached" in (last["error"] or "")
    assert last["approved"] is True  # 失败也是"批准后执行的"，审计要如实
    assert "执行失败" in wf.resume_notice


async def test_resume_reuses_paused_decision_blob(monkeypatch):
    """续跑这一行要沿用暂停那一行的 decision，不能写成空快照。

    回放器靠 decision 重建"当时这一问"。写成空的话它只会看到 snapshot=None，
    以为数据丢了——而实际上这一问在审批之前就问过了。
    顺带：元素重新定位也全靠这份快照里的 role/text。
    """
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow()
    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    blob = wf.trace[-1]["decision"]
    assert blob["snapshot_text"] == "第 4 步的快照正文"
    assert blob["payload"] == {"action": "click", "target": "e3"}
    assert blob["plan"] == ["填姓名"]


async def test_normal_start_ignores_resume_path():
    """没有 resume 参数时仍走原来的开场：goto(start_url)。"""
    session = _FakeSession()
    wf = BrowserAgentWorkflow(session=session, llm=_FakeLLM())  # type: ignore[arg-type]

    await wf.step_start(None, StartEvent(goal="打开首页", start_url=_PAUSE_URL))  # type: ignore[arg-type]

    assert session.calls == [("goto", _PAUSE_URL)]
    assert wf.trace == []
    assert wf.goal == "打开首页"


@db_required
async def test_decide_approval_is_compare_and_swap(seeded):
    """同一审批项只能被决定一次：第一次 True，第二次 False。

    这是并发保护的核心——两个人同时点"批准"，第二个必须拿不到。
    没有这一层，任务会被续跑两次，对"提交订单"就是重复下单。
    """
    approval_id = await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url=_PAUSE_URL, context={},
    )
    assert await repo.decide_approval(
        approval_id, status=APPROVAL_APPROVED, decided_by=seeded["alice"], note="ok"
    )
    # 第二次：WHERE status='pending' 不命中 → rowcount 0
    assert not await repo.decide_approval(
        approval_id, status=APPROVAL_APPROVED, decided_by=seeded["bob"]
    )
    # 审批人不能被第二次覆盖（否则审计链说不清是谁批的）
    row = await repo.fetch_approval(approval_id)
    assert row["status"] == APPROVAL_APPROVED
    assert row["decided_by"] == seeded["alice"]
    assert row["note"] == "ok"


@db_required
async def test_revert_approval_makes_it_pending_again(seeded):
    """入队失败时把审批项退回去，用户才能重试。

    这是"界面什么都没错、任务就是不动"那个死法的唯一解药：
    只退任务状态、不退审批项的话，前端拿不到 pending，用户连再点一次的入口都没有。
    """
    approval_id = await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url=_PAUSE_URL, context={},
    )
    assert await repo.decide_approval(
        approval_id, status=APPROVAL_APPROVED, decided_by=seeded["alice"], note="ok"
    )
    assert await repo.fetch_pending_approval(seeded["task"]) is None

    assert await repo.revert_approval(approval_id)

    pending = await repo.fetch_pending_approval(seeded["task"])
    assert pending is not None
    assert pending["status"] == APPROVAL_PENDING
    # 审批人/意见要一起清掉：这一条现在回到"没人处理过"的状态
    assert pending["decided_by"] is None
    assert pending["note"] == ""


@db_required
async def test_fetch_approval_by_id_ignores_other_tasks(seeded):
    """按 id 取审批项：这是续跑定位"批的是哪一条"的唯一方式。

    换成"取任务最新的 pending"会在"批完第 4 步、续跑到第 7 步又触发审批"时
    取错——worker 会以为第 7 步那条是刚批准的动作，于是执行一个没人批过的动作。
    """
    id4 = await repo.create_approval(
        task_id=seeded["task"], step_no=4, action={"action": "click", "target": "e3"},
        reason="高风险", snapshot_url=_PAUSE_URL, context={},
    )
    id7 = await repo.create_approval(
        task_id=seeded["task"], step_no=7, action={"action": "click", "target": "e7"},
        reason="高风险", snapshot_url=_PAUSE_URL, context={},
    )
    assert (await repo.fetch_approval(id4))["step_no"] == 4
    assert (await repo.fetch_approval(id7))["step_no"] == 7
    assert await repo.fetch_approval(999_999_999) is None


# ---------------------------------------------------------- 6. 现场还原（方案 D）


def _patch_snapshot_seq(monkeypatch, snapshots: list[list[dict]]) -> None:
    """按调用顺序返回不同的快照。

    为什么需要"序列"：续跑会拍两次快照——一次是刚 goto 出来的空页面，
    一次是回填之后用来核对的。两次内容不同，正是要用它验证
    "回填前是空的、回填后对上了"。
    """
    queue = list(snapshots)

    async def fake_snapshot(_page):
        items = queue.pop(0) if queue else snapshots[-1]
        return _make_snapshot(items)

    monkeypatch.setattr("app.agent.workflow.take_snapshot", fake_snapshot)


_FORM_STATE = [
    {"role": "input", "text": "姓名", "value": "张三", "restorable": True},
    {"role": "select", "text": "城市", "value": "福州", "restorable": True},
]

_EMPTY_FORM = [
    {"id": "e1", "role": "input", "text": "姓名", "value": ""},
    {"id": "e2", "role": "select", "text": "城市", "selected_text": "请选择"},
    {"id": "e5", "role": "button", "text": "提交"},
]

_FILLED_FORM = [
    {"id": "e1", "role": "input", "text": "姓名", "value": "张三"},
    {"id": "e2", "role": "select", "text": "城市", "selected_text": "福州"},
    {"id": "e5", "role": "button", "text": "提交"},
]


async def test_gate_captures_form_state():
    """闸门停下时要把表单内容一起冻结——这是续跑能"还原现场"的唯一来源。"""
    snapshot = _make_snapshot(
        [
            {"id": "e1", "role": "input", "text": "姓名", "value": "张三", "input_type": "text"},
            {"id": "e2", "role": "select", "text": "城市", "selected_text": "福州"},
            {"id": "e3", "role": "checkbox", "text": "订阅", "checked": True},
            {"id": "e4", "role": "input", "text": "备注", "value": "", "input_type": "text"},
            {"id": "e9", "role": "button", "text": "提交"},
        ]
    )
    wf = _workflow()
    wf.snapshot = snapshot  # type: ignore[assignment]

    out = await wf.step_gate(None, GateEvent(action=_click("e9")))  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    state = out.result["approval"]["context"]["form_state"]
    by_text = {f["text"]: f for f in state}
    assert by_text["姓名"]["value"] == "张三"
    assert by_text["城市"]["value"] == "福州"
    assert by_text["订阅"]["value"] is True
    # 空字段不记：撑大 context 又会在核对时制造噪音
    assert "备注" not in by_text
    assert all(f["restorable"] for f in state)


async def test_gate_marks_password_field_unrestorable():
    """密码框只记"有值但不能还原"，绝不记真实值。

    明文密码不进审批表是硬约束。代价是含密码的任务续跑会被拒绝——
    这个取舍是对的：宁可不动，也不能带着空密码去提交。
    """
    snapshot = _make_snapshot(
        [
            {"id": "e1", "role": "input", "text": "密码", "value": "***", "input_type": "password"},
            {"id": "e9", "role": "button", "text": "提交"},
        ]
    )
    wf = _workflow()
    wf.snapshot = snapshot  # type: ignore[assignment]

    out = await wf.step_gate(None, GateEvent(action=_click("e9")))  # type: ignore[arg-type]

    field = out.result["approval"]["context"]["form_state"][0]
    assert field["restorable"] is False
    assert field["value"] == ""      # 连 *** 都不抄进 context
    assert "密码" in field["reason"]


async def test_resume_restores_form_before_executing(monkeypatch):
    """续跑要先回填、核对通过，才执行被批准的动作。

    顺序反了就是这个事故：在空表单上点提交 → "提交成功：姓名=，邮箱=，城市=未填"。
    """
    # 拍两次：第一次是 goto 出来的空页面，第二次是回填后的页面
    _patch_snapshot_seq(monkeypatch, [_EMPTY_FORM, _FILLED_FORM])
    session = _FakeSession()
    wf = _resume_workflow(session, context={**_resume_state()["context"], "form_state": _FORM_STATE})

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    kinds = [c[0] for c in session.calls]
    assert kinds == ["goto", "type", "select", "click"], kinds
    assert "张三" in session.calls[1][1]
    assert "福州" in session.calls[2][1]
    assert "e5" in session.calls[3][1]     # 批准的动作用的是重新定位后的编号
    assert not isinstance(out, StopEvent)
    assert wf.trace[-1]["approved"] is True
    assert wf.trace[-1]["ok"] is True


async def test_resume_gives_up_when_password_cannot_be_restored(monkeypatch):
    """含密码的任务续跑直接放弃——不能带着空密码去提交。"""
    _patch_snapshot_seq(monkeypatch, [_EMPTY_FORM, _EMPTY_FORM])
    session = _FakeSession()
    wf = _resume_workflow(
        session,
        context={
            **_resume_state()["context"],
            "form_state": [
                {"role": "input", "text": "密码", "value": "", "restorable": False},
            ],
        },
    )

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert [c[0] for c in session.calls] == ["goto"]   # 一个字段都没填、没点
    assert not isinstance(out, StopEvent)
    assert "敏感字段" in wf.resume_notice
    assert wf.trace[-1]["resume_skipped"] is True


async def test_resume_gives_up_when_verification_fails(monkeypatch):
    """回填之后回读发现值不对 → 放弃执行。

    为什么要回读：fill / select_option 都是"发出去就算成功"的调用，
    页面自带 onchange 可能把值改掉。**动作成功 ≠ 做对了**。
    """
    # 两次都给空页面：fill 调了，但页面没记住 → 核对失败
    _patch_snapshot_seq(monkeypatch, [_EMPTY_FORM, _EMPTY_FORM])
    session = _FakeSession()
    wf = _resume_workflow(session, context={**_resume_state()["context"], "form_state": _FORM_STATE})

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert "click" not in [c[0] for c in session.calls]   # 批准的动作没执行
    assert not isinstance(out, StopEvent)
    assert "核对失败" in wf.resume_notice
    assert wf.trace[-1]["resume_skipped"] is True


async def test_resume_without_form_state_still_works(monkeypatch):
    """没有表单内容的页面（纯列表页）不该被这一步挡住。"""
    _patch_snapshot(monkeypatch)   # 只有 e2/e5，没有 form_state
    session = _FakeSession()
    wf = _resume_workflow(session)

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert [c[0] for c in session.calls] == ["goto", "click"]
    assert wf.trace[-1]["ok"] is True


# ---------------------------------------------------------- 7. 拒绝续跑（P5a-4）


def _rejected_state(**overrides) -> dict:
    """拒绝时 resume_state 多出来的两个键。

    `rejected` 用布尔而不是再立一套 approve/reject 词表：审批项状态已经是唯一真相，
    多一套词就多一处会漂移的映射。`note` 在批准时是审计备注、在拒绝时是**会进 prompt
    的语义输入**——同一个库列两种用法，由 rejected 区分。
    """
    state = {"rejected": True, "note": "不要提交，改成只读列表"}
    state.update(overrides)
    return state


async def test_resume_rejected_executes_nothing(monkeypatch):
    """拒绝的语义只有一条：**什么都不执行**，只回现场。

    "批准"和"拒绝"最危险的混淆不是文案，而是代码——要是 reject 也走一遍 _apply，
    用户就会看到"我明明拒绝了，它还是点了提交"。所以这条断言的重点是
    calls 里只有 goto，一个 click / type 都没有。
    """
    _patch_snapshot(monkeypatch)
    session = _FakeSession()
    wf = _resume_workflow(session, **_rejected_state())

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert [c[0] for c in session.calls] == ["goto"]
    assert not isinstance(out, StopEvent)      # 不是失败，是"让模型重新决策"
    last = wf.trace[-1]
    assert last["rejected"] is True
    # 和 approved 互斥：一行要么是"人让做的"，要么是"人否了"，不可能都是
    assert "approved" not in last
    assert last["ok"] is False                 # 没执行，就不能记成功
    assert "不要提交" in (last["error"] or "")
    assert wf.llm.last_usage == {}              # 这一趟没问模型


class _ScriptedLLM:
    """给出固定输出的假模型，同时记住"这次被问了什么"。

    为什么不能只看 trace：这一层要验的是**拒绝理由有没有送到模型眼前**，
    而最接近真相的证据就是实际拼出来的那段 prompt。
    断言 notice / 内部字段都只是间接证据——字段对了但没渲染进 prompt，
    模型照样看不见（P5a-4 第一版就是这么翻的车）。
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.last_usage: dict = {}
        self.seen: list[str] = []

    async def decide(self, _system: str, user: str) -> dict:
        self.seen.append(user)
        return self.payload


async def test_resume_rejected_tells_model_why(monkeypatch):
    """拒绝理由必须出现在**实际拼出来的 prompt** 里，而且位置在快照之前。

    这条是 P5a-4 第一版翻车后补的：第一版把理由塞进一次性 notice，
    模型连续 5 轮照旧点提交——因为它每轮重读"任务"（目标里写着"点击提交按钮"），
    而末尾那句"观察"在它眼里不如目标硬。所以断言从"notice 里有"改成
    "prompt 里有、且在 [用户否决] 段、位置靠前"。
    """
    _patch_snapshot(monkeypatch)
    llm = _ScriptedLLM({"thought": "按用户说的读按钮", "action": "finish", "answer": "提交"})
    wf = BrowserAgentWorkflow(
        session=_FakeSession(), llm=llm, resume=_resume_state(**_rejected_state())  # type: ignore[arg-type]
    )

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]
    ev = await wf.step_observe(None, ObserveEvent())  # type: ignore[arg-type]
    await wf.step_decide(None, ev)  # type: ignore[arg-type]

    prompt = llm.seen[-1]
    assert "[用户否决]" in prompt
    assert "不要提交" in prompt
    assert "优先于上面的任务" in prompt
    # 位置：必须排在快照之前。排在快照后面就等于又被埋进长正文里了
    assert prompt.index("[用户否决]") < prompt.index("资料填写")
    # 冻结进 decision：回放器靠它重建同一份 prompt
    # 注意看的是"下一次决策会写进去的那份"（_decision_inputs），
    # 不是 trace[-1]——那是**被拒绝那一行**，它的 decision 是暂停时的原始提问，
    # 那时还没有否决段落，所以理应为空。两者别搞混。
    assert "不要提交" in wf._decision_inputs.get("rejection", "")
    assert "不要提交" in wf._decision_blob().get("rejection", "")


async def test_rejection_hint_is_not_one_shot(monkeypatch):
    """否决段要**每轮都带**，不能像 notice 那样说完就清。

    这是和 notice 的关键区别：notice 是"这一轮发生了什么"，
    拒否是"从今往后的约束"。搞混了两者的生命周期，就等于让模型下一轮就忘了。
    """
    _patch_snapshot(monkeypatch)
    llm = _ScriptedLLM({"thought": "读按钮", "action": "finish", "answer": "ok"})
    wf = BrowserAgentWorkflow(
        session=_FakeSession(), llm=llm, resume=_resume_state(**_rejected_state())  # type: ignore[arg-type]
    )

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]
    ev1 = await wf.step_observe(None, ObserveEvent())  # type: ignore[arg-type]
    await wf.step_decide(None, ev1)  # type: ignore[arg-type]
    ev2 = await wf.step_observe(None, ObserveEvent())  # type: ignore[arg-type]
    await wf.step_decide(None, ev2)  # type: ignore[arg-type]

    assert len(llm.seen) == 2
    assert "[用户否决]" in llm.seen[0]
    assert "[用户否决]" in llm.seen[1]      # 第二轮还在
    # 一次性提示（notice）这时应该已经被清掉了，两者互不影响
    assert wf.resume_notice == ""


async def test_resume_rejected_without_note_falls_back(monkeypatch):
    """不填理由也要能拒：用一句默认说明兜底，而不是留空让模型猜。"""
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow(**_rejected_state(note=""))

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert "未填理由" in (wf.trace[-1]["error"] or "")
    text = wf._rejection_text()
    assert "用户的理由" not in text           # 没理由就别编一段出来
    assert "不要重复那个动作" in text


async def test_rejected_action_is_blacklisted(monkeypatch):
    """刚被拒的动作又冒出来 → 不再问人，直接终止。

    为什么要有这条（P5a-4 实测）：拒绝之后模型每次都在**新的一步**重新提"提交"，
    于是每拒一次它就再停一次，脚本连拒 4 次还没停。光靠"理由进 prompt"
    治不住这种固执，必须有一个硬出口，否则人会被同一个问题反复打断。
    """
    _patch_snapshot(monkeypatch)   # 新页面里是 e5「提交」
    wf = _resume_workflow(**_rejected_state())
    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]
    assert wf.rejected_actions == ["click:提交"]

    # 模型换了个步号、编号也变了，但「提交」还是那个「提交」
    wf.snapshot = _make_snapshot(
        [
            {"id": "e9", "role": "input", "text": "姓名"},
            {"id": "e12", "role": "button", "text": "提交"},
        ]
    )  # type: ignore[assignment]

    out = await wf.step_gate(None, GateEvent(action=_click("e12")))  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    assert out.result["status"] == "loop"
    assert "已经拒绝过" in out.result["note"]


async def test_blacklist_does_not_block_other_actions(monkeypatch):
    """黑名单只挡被拒的那一个动作，别的高风险动作照常问人。

    挡宽了就等于"拒一次，后面所有需要审批的动作都被静默放行/直接判死"，
    那是把一个精确的约束变成了全局开关。
    """
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow(**_rejected_state())
    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    wf.snapshot = _make_snapshot(
        [
            {"id": "e7", "role": "button", "text": "删除"},
            {"id": "e12", "role": "button", "text": "提交"},
        ]
    )  # type: ignore[assignment]

    async def fake_cb(_pending: dict) -> None:
        return None

    wf.on_approval = fake_cb
    out = await wf.step_gate(None, GateEvent(action=_click("e7")))  # type: ignore[arg-type]

    # 删除也是高风险 → 停下来问人，而不是被当成"重复动作"判死
    assert isinstance(out, StopEvent)
    assert out.result["status"] == "waiting_approval"


async def test_gate_persists_rejection_state_for_next_pause(monkeypatch):
    """否决信息要跟着 context 走，才能跨进程活下来。

    续跑会重开浏览器、可能落到另一个 worker 进程。不把这两个字段冻进 context，
    续跑之后模型就能把被拒过的动作重新提一遍——"人已经说过不行"不该因为
    换了个进程就失效。
    """
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow(**_rejected_state())
    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    wf.snapshot = _make_snapshot([{"id": "e7", "role": "button", "text": "删除"}])  # type: ignore[assignment]

    async def fake_cb(_pending: dict) -> None:
        return None

    wf.on_approval = fake_cb
    out = await wf.step_gate(None, GateEvent(action=_click("e7")))  # type: ignore[arg-type]

    ctx = out.result["approval"]["context"]
    assert ctx["rejected_actions"] == ["click:提交"]
    assert any("不要提交" in h for h in ctx["rejection_hints"])


async def test_resume_rejected_keeps_trace_continuous(monkeypatch):
    """拒绝同样是"接着跑"：前 N 行必须原样接上，步号不能重来。"""
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow(**_rejected_state())

    await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert len(wf.trace) == 5
    # 第 4 步出现两次：一次是暂停那行，一次是"被拒绝"这行（同一步的两个尝试，
    # 靠 task_steps.attempt 区分，和"决策被拒绝后重试"是同一套处理）
    assert [r["step"] for r in wf.trace] == [1, 2, 3, 4, 4]
    assert wf.trace[3]["pending_approval"] is True   # 暂停那行还在
    assert wf.step_no == 4                           # 步号不从头来
    # 冻结的决策输入沿用暂停那份：回放器才重建得出"当时这一问"
    assert wf.trace[-1]["decision"]["payload"] == {"action": "click", "target": "e3"}


async def test_resume_rejected_still_restores_form(monkeypatch):
    """拒绝不代表表单白填：回填现场，模型才有"东西"可确认。

    用户提"先确认城市选对了再提交"时靠的就是这一步——表单空着，模型只能重填，
    而重填出来的值不一定和上次一样，于是又被拒一次，白烧两轮。
    注意最后**没有 click**：回填是为了给模型看，不是为了执行动作。
    """
    _patch_snapshot_seq(monkeypatch, [_EMPTY_FORM, _FILLED_FORM])
    session = _FakeSession()
    wf = _resume_workflow(
        session,
        context={**_resume_state()["context"], "form_state": _FORM_STATE},
        **_rejected_state(),
    )

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert [c[0] for c in session.calls] == ["goto", "type", "select"]
    assert "张三" in session.calls[1][1]
    assert "福州" in session.calls[2][1]
    assert not isinstance(out, StopEvent)
    assert wf.trace[-1]["rejected"] is True


async def test_resume_rejected_survives_unrestorable_form(monkeypatch):
    """回填不了也不阻断拒绝——反正不执行动作。

    和批准路径的区别正好在这里：那边回填不了就必须放弃（否则等于在错的现场执行），
    这边只是少一份参考资料，模型照样能重新看页面。
    """
    _patch_snapshot_seq(monkeypatch, [_EMPTY_FORM, _EMPTY_FORM])
    session = _FakeSession()
    wf = _resume_workflow(
        session,
        context={
            **_resume_state()["context"],
            "form_state": [
                {"role": "input", "text": "密码", "value": "", "restorable": False},
            ],
        },
        **_rejected_state(),
    )

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert "click" not in [c[0] for c in session.calls]
    assert not isinstance(out, StopEvent)       # 不判死
    assert "敏感字段" in wf.resume_notice       # 但要说清现场没还原
    assert wf.trace[-1]["rejected"] is True     # 关键：拒绝这件事照样成立


async def test_resume_rejected_with_broken_action_stops(monkeypatch):
    """动作数据坏到还原不出来 → 诚实失败，不编一句"上一步被拒了"糊过去。

    拒绝路径不执行动作，看着"不需要那个动作也能继续"；但它是唯一能告诉模型
    "被拒的是什么"的东西，也是这条 trace 的骨架。连它都没了，说明审批行已经坏了，
    这时候继续跑只会让人以为自己的决定生效了。
    """
    _patch_snapshot(monkeypatch)
    wf = _resume_workflow(**_rejected_state(action={"action": "不存在的动作"}))

    out = await wf.step_start(None, StartEvent())  # type: ignore[arg-type]

    assert isinstance(out, StopEvent)
    assert out.result["status"] == "bad_request"
    assert "无法还原" in out.result["note"]


async def test_route_reject_marks_rejected_and_resumes(monkeypatch):
    """拒绝要写成 rejected（不是 approved），并且**照样**重新入队续跑。

    这条防的是最坏的一种错：把 decision 忽略了、一律写成 approved——
    用户点了拒绝，动作还是被执行，而且审计记录上写着"批准"。
    纯映射逻辑，不碰数据库，所以用 monkeypatch 直接调路由函数。
    """
    from app.api import approvals as api_mod

    seen: dict = {}

    async def fake_get_task(_task_id):
        return {"status": "waiting_approval", "user_id": 1}

    async def fake_pending(_task_id, user_id=None):
        return {"id": 77, "step_no": 4}

    async def fake_decide(approval_id, *, status, decided_by=None, note=""):
        seen.update(approval_id=approval_id, status=status, note=note)
        return True

    async def fake_resume(task_id, approval_id):
        seen["resumed"] = (task_id, approval_id)

    monkeypatch.setattr(api_mod.task_runner, "get_task", fake_get_task)
    monkeypatch.setattr(api_mod.repo, "fetch_pending_approval", fake_pending)
    monkeypatch.setattr(api_mod.repo, "decide_approval", fake_decide)
    monkeypatch.setattr(api_mod.task_runner, "resume", fake_resume)

    # user=None → assert_owned 放行（本地模式），这条测的是映射、不是隔离
    out = await api_mod.decide_approval(
        "t-x", api_mod.DecisionRequest(decision="reject", note="不要提交"), None
    )

    assert seen["status"] == "rejected"          # 不是 approved
    assert seen["note"] == "不要提交"             # 理由要落库：模型靠它重新决策
    assert seen["resumed"] == ("t-x", 77)        # 拒绝同样要入队
    assert out["decision"] == "reject"
    assert out["task_status"] == "queued"
