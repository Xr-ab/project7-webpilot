"""Agent 循环本体：observe → decide → act，转到做完或者必须停。

这里用 LlamaIndex Workflows，而不是 FunctionAgent / ReAct，原因就一句话：
**循环归自己写**。FunctionAgent 是"框架拿着工具列表自己转圈"，你只能通过 prompt 影响它；
Workflows 是"你写 step，框架只负责把一个 step 的输出事件投递给下一个能处理它的 step"。
多一步、少一步、什么时候停、失败怎么归因，全在我们手里——这是能排查问题的前提。

五个 step 和四种事件的关系（注意最后那条回边，它就是"循环"）：

    StartEvent ──► step_start   （打开起始页）
                       │ 返回 ObserveEvent
                       ▼
    ObserveEvent ─► step_observe（拍快照）──► DecideEvent
                                                │
    DecideEvent ──► step_decide（问模型 + 校验）──► GateEvent
                                                │（校验不过则回退成 DecideEvent 重试一次）
    GateEvent ────► step_gate  （审批判定，P5a）──► ActEvent（放行）
                                                └─► StopEvent(waiting_approval)（命中风险）
    ActEvent ─────► step_act   （执行动作）──► ObserveEvent（再转一圈）
                                          └─► StopEvent（结束）

> 为什么 gate 是独立一步而不是塞进 decide：见 GateEvent 的注释——
> 两个 step 声明消费同一事件会被并发触发，动作会抢在判定之前落地。

**续跑入口（P5a-3 / P5a-4）**：同一个 workflow 类带 `resume=` 参数启动时，step_start 走另一条分支。
**批准**和**拒绝**走的是同一条路——浏览器都死了，所以"重开浏览器 + 回到暂停页 + 接上步号"
对两者是同一个需求，只在最后一公里分叉：

    StartEvent(resume=...) ──► step_start
                                  ├─ goto(暂停时的页面) + 接上 trace / 步号 / plan
                                  └─ 分叉
                                       ├─ 批准 ─► 按 role+文字重新定位 ─► 执行它 ─► ObserveEvent
                                       └─ 拒绝 ─► 什么都不执行 ─► 记一行 rejected
                                                     └─► 把否决写进 rejection_hints（每轮都带上）
                                                     └─► 把动作写进 rejected_actions（再犯就判 loop）

为什么"批准"不等于"再问一次模型"：人批的是**一个具体动作**（点 e3 这个提交按钮）。
重新问模型很可能给出另一个动作，那就变成"人批 A、机器执行 B"，审批就形同虚设。
反过来，"拒绝"恰恰**必须**重新问模型——人否的是这个动作，不是整个任务。
如果拒绝等于把任务判死，用户面对审批就只有"批"和"白跑一遍"两个选项，
久了必然闭眼点批准，闸门退化成橡皮图章。

退出条件共 6 个，各自防一类事故（缺一个就会有一类事故说不清原因）：
- success          模型 finish      正常完成
- step_limit       走满 max_steps   模型原地绕圈
- timeout          Workflow 超时    页面卡死 / 单步挂住
- loop             同动作连续 3 次   死循环（最常见，光靠步数上限发现太晚）
- invalid_action   连续 2 次非法   模型读不懂快照，再试也没用
- waiting_approval 命中审批规则     高风险动作等人批（不是失败，见 app/status.py）
"""
from __future__ import annotations

import difflib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from app.agent import vision
from app.agent.actions import (
    Action,
    ActionError,
    ActionType,
    check_action,
    describe,
    parse_raw,
)
from app.agent.approval import gate as approval_gate
from app.agent.approval import is_password_input
from app.agent.llm import LLMClient, LLMFormatError
from app.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.browser.session import BrowserSession
from app.browser.snapshot import (
    PageSnapshot,
    page_visual_cues,
    render_snapshot_text,
    selector_for,
    take_snapshot,
)
from app.config import settings
from app.security import vault
from app.security.vault import VaultError

# 同一个动作连续出现多少次判定为死循环。
# 定 3 不给 2：模型偶尔会"再点一次确认"，两次是正常行为，三次就是卡住了。
LOOP_THRESHOLD = 3

# 连续几次非法动作就放弃。给 2 次机会：第一次多是自己编了个编号，回喂错误后能改对。
MAX_INVALID_ACTIONS = 2

# 一个任务最多让视觉模型看几次图（P6a）。
# 为什么要上限：触发条件 2（连续失败）在"页面本来就搞不定"的任务上会步步命中，
# 每步多花 3~8 秒 + 一份免费额度，而重复看同一张图得不到新信息。
# 2 次够覆盖"第一次看不懂 → 看图 → 还不行 → 再看一眼"的真实场景。
VISION_MAX_CALLS = 2

# 快照完全是空白时，重拍几次、每次等多久。
# 为什么要有这个：真实站点换页瞬间，浏览器会先清空旧文档、新内容还没画出来，
# 这时候拍到的画面是纯白的、元素数 0、正文空。
# 重试 3 次 × 400ms ≈ 1.2 秒上限——足够覆盖普通页面渲染，又不至于在真死页上白等。
EMPTY_RETRY = 3
EMPTY_WAIT_MS = 400


class ObserveEvent(Event):
    """该看页面了。"""


class DecideEvent(Event):
    """看完了，请求决策。

    error_hint：上一轮动作被拒的原因。带上它重试，模型才有机会改对；
    不带的话它会拿着同样的信息再犯一遍。
    """

    snapshot_text: str
    error_hint: str = ""
    notice: str = ""


class ActEvent(Event):
    """决策通过、审批也通过，去执行。"""

    action: Action


class GateEvent(Event):
    """决策出来了，先过审批闸门（P5a）。

    为什么要单独一个事件，而不是让 decide 直接返回 ActEvent：
    LlamaIndex Workflows 按**事件类型**路由 step。如果 step_gate 和 step_act 都声明
    消费 ActEvent，两步会被同时触发（并发执行），动作会抢在判定之前落地——
    那闸门就等于不存在。所以 decide 发 GateEvent，gate 判定通过后再发 ActEvent，
    act 的入口类型一行都不用改。

    action：模型给出的动作（已通过形状/语义/白名单三层校验）。
    """

    action: Action


def _text_delta(old: str, new: str, limit: int = 120) -> str:
    """算出"新页面上多出来的那段文字"。

    为什么要这个：动作生效后，结果往往只是正文末尾多了一行
    （"提交成功：姓名=张三…"）。模型经常看不见这行，于是把已经成功的动作再做一遍——
    T2 第一次跑就是这样连点了 3 次"提交"。我们替它把差异挑出来，它才能确认"我做到了"。

    只挑"新增"的部分，删掉的内容不提示：对任务完成来说，"多了什么"才是关键信号。
    """
    if not old or not new:
        return ""
    if old == new:
        return ""
    # 最常见的形态：内容追加在末尾（innerText 就是这么排的）
    if new.startswith(old):
        return new[len(old) :].strip()[:limit]

    added: list[str] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.append(new[j1:j2])
    return " ".join(part.strip() for part in added if part.strip())[:limit]


def _is_blank(snapshot: PageSnapshot | None) -> bool:
    """判断快照是不是"什么都没看到"。

    为什么用"元素数 0 **且** 正文为空"两个条件，而不是只看元素数：
    - 只有元素数为 0：可能是一个纯文字页（文章正文、结果页），这是正常的，
      模型仍然可以读完正文直接作答，不该打扰它。
    - 两个都为空：这个页面根本没有内容 —— 99% 是还在渲染，或者跳转失败了。
      这时候模型拿到的是"一张白纸"，它会开始编不存在的编号。
    """
    if not snapshot:
        return True
    if snapshot.get("elements_total"):
        return False
    return not (snapshot.get("text") or "").strip()


class BrowserAgentWorkflow(Workflow):
    """一个实例只跑一个任务。跑完把 trace 一起交出去。"""

    def __init__(
        self,
        *,
        session: BrowserSession,
        llm: LLMClient,
        max_steps: int | None = None,
        timeout_s: int | None = None,
        shot_dir: Path | None = None,
        on_step: Callable[[list[dict]], Awaitable[None]] | None = None,
        on_approval: Callable[[dict], Awaitable[None]] | None = None,
        approval_enabled: bool = True,
        resume: dict | None = None,
        vision: bool = False,
    ) -> None:
        # 兜底超时交给框架：万一某个 await 卡死不返回，至少能整体中断
        self.max_steps = max_steps or settings.max_steps
        self.timeout_s = timeout_s or settings.task_timeout_seconds
        super().__init__(timeout=self.timeout_s)

        self.session = session
        self.llm = llm
        # 传了目录就每步截图（M5 回放用）；不传就不截，脚本调试时更快
        self.shot_dir = shot_dir
        # 每记完一步回调一次，让调用方把 trace 落盘 —— 前端才能"实时"看到步骤冒出来
        self.on_step = on_step
        # 需要人工审批时回调一次，把审批信息交给调用方落库（P5a）。
        # 为什么用回调而不是让 workflow 直接写 DB：workflow 只应该知道"要停"，
        # 不该知道数据存哪。这样单测可以塞一个假回调，不碰数据库。
        self.on_approval = on_approval
        # 审批闸门总开关 = 进程级 AND 任务级。
        # 为什么是"与"而不是"或"：进程级（APPROVAL_ENABLED）是紧急止血闸，
        # 一旦关掉必须对所有任务生效；任务级（require_approval）是批处理的免批声明，
        # 只能在总闸开着时给自己免批，不能反过来强行要求审批。
        self.approval_enabled = bool(settings.approval_enabled and approval_enabled)

        # ---- P6a 视觉兜底的状态 ----
        # 任务级强制开关（payload vision=true）：canvas 演示/评测对比用。
        # 注意它只在进程级 vision_enabled 打开时才有意义——总闸关着时谁也不能看，
        # 理由和 approval 的"与"逻辑一样：总闸是止血闸，必须能一票否决。
        self.vision_forced = bool(vision)
        # 连续动作失败步数：视觉兜底条件 2 的判据（_apply 里维护）
        self.consecutive_failures = 0
        # 本任务已看过几次图（上限 VISION_MAX_CALLS）
        self.vision_calls = 0
        # 本轮观察阶段产生的视觉记录，_record 时挂到 trace 行上然后清空。
        # 为什么挂在行上而不是单独存表：统计"视觉兜底率"只需按步数聚合，
        # 而回放器要能回答"这一步模型看到的描述是什么"——它必须和历史同帧。
        self._vision_record: dict | None = None

        # 以下都是"一次运行的状态"，不放全局
        self.goal = ""
        self.step_no = 0
        self.trace: list[dict] = []
        self.answer = ""
        self.invalid_actions = 0
        self.snapshot: PageSnapshot | None = None
        self.current_url = ""
        # 上一轮的快照文本：用来判断"这一步动作是不是白做了"
        self.last_snapshot_text = ""
        # 上一轮的地址：用来判断"是不是跳到别的页面了"。
        # 为什么文本比对了还要看地址（P7-D1 T9 实测）：点击链接跳页时整页文字都换掉了，
        # 只报"新出现的内容"给到模型的是『2 （第 2 页） 上一 tail 返回首 in x』这种碎片，
        # 它读不出"我已经换了页面、元素编号全重排了"，于是继续按上一页的编号点，
        # 结果点了 3 次「上一页」在原路来回晃。地址变化是这件事最省字、最没有歧义的表达。
        self.last_url = ""
        # 动作回显：上一轮执行完，把"你实际操作到的那个元素是什么文字"记下来，
        # 下一轮拼进 notice 后清空。
        # 为什么需要（P7-D1 T9 实测）：模型会先立一个意图（"点下一页"），然后去元素列表里
        # **随便挑一个编号**执行，全程不核对编号后面跟着的文字。实测原样是：快照里明明写着
        # `e1 link 上一页 -> detail.html`，它的 thought 是"点击下一页按钮"，照样点 e1，
        # 于是一路往回翻，最后在**第一页**上回答了"下一页还能点"。
        # 光在快照里写清楚是不够的（它就在眼前，模型也不看），必须在动作执行**之后**
        # 告诉它"你刚点的是『上一页』"——把结果怼到它脸上，它才有机会自己发现走反了。
        self.action_echo = ""
        # 多步任务的任务分解（模型自己在第一轮给出）+ 已完成条目。
        # 为什么要存在我们这边：模型每轮都是"失忆"的，只有把它自己给过的 plan 重新喂回去，
        # 它才知道"哪条还没做"——这是修"多步子任务被压成一步"的关键。
        self.plan: list[str] = []
        self.progress: list[int] = []
        # 本轮决策的输入（step_decide 开头冻结）+ 模型原始输出。
        # M5a 回放器靠它们把"当时这一问"原样重建出来——
        # 改 prompt 之后重放历史，就能对比"老 prompt vs 新 prompt"谁答得对。
        self._decision_inputs: dict = {}
        self._last_payload: dict | None = None
        # 待审批信息（P5a）。非 None 表示这次运行是被闸门停下来的，
        # 里面带着恢复续跑所需的全部上下文（动作 + 计划 + 当前 URL + 步号）。
        self.pending_approval: dict | None = None
        # 一次性提示（P5a-3 / P5a-4）：只在本轮说一次的事情——
        # "被批准的动作没执行成"、"现场没还原全"。拼进 notice 后立刻清空。
        self.resume_notice = ""
        # 用户否决过什么（P5a-4）。两个用途，缺一不可：
        #
        # ① rejection_hints：**每轮都要带上**的硬约束，渲染在 prompt 的 [用户否决] 段。
        #    为什么不能只用一次性的 resume_notice：P5a-4 实测，那个任务的目标本身写着
        #    "然后点击提交按钮"，模型每轮重读目标、于是每轮重新点提交，连续 5 轮被拒 5 次。
        #    它不是没看到拒绝，而是认为"目标"比"一句观察"更硬。所以必须升到目标同级。
        #
        # ② rejected_actions：动作黑名单（按"类型+元素文字"，不含编号——浏览器重开后
        #    编号会变）。模型要是坚持执行一个刚被拒的动作，就不再问人了，直接判 loop 收尾。
        #    为什么不再问一次：同一个问题连着问人两遍，人只会觉得它没听懂人话；
        #    而且这等于给了"无限审批乒乓"一个没有出口的入口（实测能连续触发 5 次）。
        self.rejection_hints: list[str] = []
        self.rejected_actions: list[str] = []
        # 续跑状态（P5a-3）。非 None 表示这一趟不是新任务，而是"人处理完审批之后接着跑"：
        # 里面带着待执行的动作（被拒绝时只是用来描述"拒的是什么"）、暂停页地址、
        # 已走到的步号、**已有的 trace**、以及这次是批准还是拒绝。
        # 为什么 trace 也要从外面传进来：浏览器活体不可序列化，唯一能跨进程传的
        # 就是"上次写到哪了"。不接上它，续跑出来的 trace 会从第 1 步重新开始，
        # 前 N 步的证据全丢——回放器和统计都建立在"一份连续的轨迹"上。
        self.resume = resume
        # 续跑时解密还原出来的密码真值（P5b-②）。非 None 只出现在"批准了往密码框
        # 输入"这一趟里：_record 清洗时拿它兜底——resume 执行动作那条 trace 行
        # 的 target 在新快照里可能已经找不到（_skip_approved），_current_secret
        # 判定不出来，没有这个兜底明文就落盘了。动作处理完立刻清空，不扩大影响面。
        self._resume_secret: str | None = None

    # ------------------------------------------------------------------ step
    @step
    async def step_start(self, ctx: Context, ev: StartEvent) -> ObserveEvent | StopEvent:
        """开场：校验任务参数、打开起始页。

        两条入口（P5a-3）：新任务打开 start_url；续跑走 _resume_start。
        分两个函数而不是在一个函数里塞 if：两段逻辑几乎不重叠
        （一个是从零开始，一个是恢复现场 + 执行批准的动作），
        混在一起会让"这条路径到底跑了什么"变得难读，而这种代码出错最难查。
        """
        if self.resume is not None:
            return await self._resume_start()

        self.goal = str(ev.get("goal") or "").strip()
        start_url = str(ev.get("start_url") or "").strip()

        if not self.goal:
            return self._stop("bad_request", note="goal 不能为空")
        if not start_url:
            return self._stop("bad_request", note="start_url 不能为空")

        try:
            await self.session.goto(start_url)  # 白名单第二道闸在这里生效
        except PermissionError as exc:
            self._record_nav_failure(start_url, str(exc))
            await self._flush()
            return self._stop("bad_url", note=str(exc))
        except Exception as exc:  # 页面打不开（服务没起、DNS 错、超时）
            note = f"{type(exc).__name__}: {exc}"
            self._record_nav_failure(start_url, note)
            await self._flush()
            return self._stop("nav_failed", note=note)

        return ObserveEvent()

    async def _resume_start(self) -> ObserveEvent | StopEvent:
        """续跑开场：恢复现场 → 回到暂停页 → 按"批准 / 拒绝"分叉。

        批准和拒绝共用这一整段（重开浏览器、回现场、接步号），只在最后一公里分叉：
        批准去执行那个动作，拒绝什么都不执行、让模型带着理由重新决策。
        为什么拒绝也要走这条路而不是"直接重跑任务"：浏览器活体已经没了，
        "回到暂停那一刻"对两者是同一个需求，只是到了现场之后要做的事不同。

        四个"必须接上"的东西，少一个都会让续跑变成另一个任务：
        goal（模型要知道在做什么）、trace（证据链不能断）、step_no（步号不能重来）、
        plan/progress（模型自己定的分解，不接上它会以为全都没做，从头再走一遍）。

        ⚠️ 恢复的代价要说清：浏览器是**新开的**，暂停前的页面状态已经没了——
        填过的表单是空的、滚动位置回到顶部、未提交的会话可能过期。
        这不是实现缺陷，而是"不在进程里挂着等审批"这个决策的必然代价
        （挂起等会让 worker 被占、进程重启即丢，见 P5-实施计划 决策 1）。
        所以回到页面之后要靠模型的下一轮 observe 自己看清现状，而不是假设一切都还在。
        """
        state = self.resume or {}
        context = state.get("context") or {}

        self.goal = str(context.get("goal") or "").strip()
        self.trace = list(state.get("trace") or [])
        self.plan = list(context.get("plan") or [])
        self.progress = list(context.get("progress") or [])
        # 之前被人否决过的动作要接上（P5a-4）。不接的话，续跑之后模型可以
        # 把同一个被拒的动作重新提一遍——而"人已经说过不行"这件事不该因为
        # 换了个 worker 进程就失效。
        self.rejection_hints = list(context.get("rejection_hints") or [])
        self.rejected_actions = list(context.get("rejected_actions") or [])
        # 步号停在 N：接下来 observe 会 +1 变成 N+1，
        # 而这一趟执行的动作记在第 N 步——和"被暂停的那一条"同号，
        # 靠 task_steps.attempt 区分（和 _record_rejected 的重试同一套处理）。
        self.step_no = int(context.get("step_no") or 0)

        # 续跑这一步没有"新的决策输入"——动作上次就定好了。把暂停那一行的
        # decision 原样接过来：否则 _record 会写下一个 snapshot=None 的空 decision，
        # 回放器读到会以为"这一问的数据丢了"，而实际上它只是被审批冻结过。
        for row in reversed(self.trace):
            blob = row.get("decision")
            if row.get("step") == self.step_no and isinstance(blob, dict):
                self._decision_inputs = blob
                self._last_payload = blob.get("payload")
                break

        url = str(state.get("snapshot_url") or context.get("current_url") or "")
        if not url:
            # 没有这个地址就没法回现场。宁可诚实失败，也不要"从起始页重跑"
            # ——那等于绕过审批，把已经发生过的动作再做一遍。
            return self._stop("bad_request", note="续跑缺少暂停页地址，无法恢复现场")

        try:
            await self.session.goto(url)  # 白名单照样生效（地址是从库里读的，也不能免检）
        except PermissionError as exc:
            self._record_nav_failure(url, str(exc))
            await self._flush()
            return self._stop("bad_url", note=str(exc))
        except Exception as exc:
            note = f"{type(exc).__name__}: {exc}"
            self._record_nav_failure(url, note)
            await self._flush()
            return self._stop("nav_failed", note=note)

        # 分叉点（P5a-4）：拒绝 = 什么都不执行，让模型带着理由重新决策。
        # 为什么放在 goto 之后而不是函数开头：两条路都要回现场，
        # 把共同部分先做掉，分叉就只剩一处——"到了现场做什么"。
        if state.get("rejected"):
            return await self._resume_rejected(state)

        raw_action = dict(state.get("action") or {})
        # 密码动作的还原（P5b-②）：库里 text 是 ***，真值只在 _secret 密文里。
        # 必须先 pop 再还原——不 pop 的话 Action(**) 虽然会忽略多余键（pydantic 默认），
        # 但依赖"默认恰好如此"是埋雷；解不开（密钥轮换/数据损坏）→ 诚实失败，
        # 绝不能把 "***" 当密码填进去——那是"动作成功但做的是错事"，
        # 和 form_state 还原不了就放弃续跑是同一个取舍。
        boxed = raw_action.pop("_secret", None)
        if boxed:
            try:
                raw_action["text"] = vault.decrypt_text(boxed)
            except VaultError as exc:
                return self._stop("bad_request", note=f"审批动作里的密码无法解密：{exc}")
            # 兜底清洗源：这条 trace 行、以及它执行失败的 resume_notice，
            # 都可能出现这个明文。动作处理完（本函数两个出口）立刻清空。
            self._resume_secret = raw_action["text"]
        try:
            action = Action(**raw_action)
        except Exception as exc:
            # 库里的动作还原不出来（字段被改坏/版本不兼容）：不能猜，直接失败
            return self._stop("bad_request", note=f"审批动作无法还原：{exc}")

        # 先拍快照再执行。原因见 _relocate_target 的说明——
        # 一句话：元素编号只在**当轮快照**里有效，而浏览器是新开的，页面上什么标记都没有。
        self.snapshot = await take_snapshot(self.session.page)
        # current_url 平时由 step_observe 从快照里更新，但续跑的执行发生在 observe 之前，
        # 不在这里补一次，这一步的 trace 行 url 会是空串——证据里就差了一个"在哪一页"。
        self.current_url = self.snapshot["url"]
        # 让下一次 observe 能把"执行后页面多了什么"告诉模型。
        # 不设的话 last_snapshot_text 是空的，模型只看到"什么都没变"，
        # 于是它会把同一个提交再点一遍（P5a-3 实测就是这么撞上第二次审批的）。
        self.last_snapshot_text = render_snapshot_text(self.snapshot)

        # ⚠️ 方案 D 的核心：goto 出来的是**一张刚打开的页面**，暂停前填的东西全没了。
        # 而这次要执行的动作（点提交）作用在表单内容上——内容不对，执行它就是在做另一件事。
        # 所以顺序必须是"先还原现场 → 再执行"，不能反。
        problem = await self._restore_form(context.get("form_state") or [])
        if problem:
            # 还原不了就诚实放弃。让模型看着现在的页面重新判断，
            # 它大概率会重新填一遍再提交——那时会**再走一次审批**，人有机会重新确认。
            outcome = await self._skip_approved(action, problem)
            self._resume_secret = None
            return outcome
        # 还原过程中可能已经重拍过快照，基准文本要跟着更新
        self.last_snapshot_text = render_snapshot_text(self.snapshot)

        if action.target:
            relocated, why = self._relocate_target(action.target)
            if relocated is None:
                # 找不到那个元素：**不执行**。宁可什么都不做，也不能"找个看着像的点了"——
                # 人批准的是"提交那个按钮"，不是"随便哪个按钮"。
                return await self._skip_approved(action, why)
            if relocated != action.target:
                action.target = relocated

        ok, error = await self._apply(action, approved=True)
        self._resume_secret = None
        if not ok:
            # 动作失败**不是**任务失败：页面可能已经变了（那个按钮不在了）。
            # 照常进循环，让模型看到这一行 ok=False 的轨迹，自己决定下一步。
            # 这也是"恢复后页面已变"这条风险的兜底——不假装成功，也不就此判死。
            # describe 里的密码就地抹掉：notice 会被冻结进下一轮 decision 落盘，
            # 而下一轮 _record 只按"下一轮动作"判定是否脱敏——下轮若是 goto 就漏了。
            described = describe(action)
            if action.text:
                described = described.replace(action.text, "***")
            self.resume_notice = (
                f"被批准的动作（{described}）执行失败：{error}。"
                "页面可能已经变化，请按当前页面重新判断。"
            )

        if action.action is ActionType.FINISH:
            return self._stop("success")
        return ObserveEvent()

    async def _resume_rejected(self, state: dict) -> ObserveEvent:
        """用户拒绝了那个动作（P5a-4）：**不执行任何东西**，带着拒绝理由让模型重新决策。

        和批准路径的唯一实质差别就是"不执行"。其余（回现场、回填表单、记 trace、
        接步号）全部照做——因为"回到暂停那一刻"对两条路是同一个需求。

        为什么拒绝也要回填表单：
        用户拒的往往是"现在就提交"，而不是"这表单填错了"。他说"先确认城市选对了再提交"时，
        模型得有东西可确认；把表单清空等于逼它重填一遍，多烧一轮 token，
        还可能填得和上次不一样，于是又被拒一次。回填失败只是记进说明、不阻断——反正不执行动作。

        为什么这条 trace 行必须打 rejected 标记：和批准路径的 approved 对称。
        事后要回答"哪些高风险动作被拦下、哪些被放行"，靠的就是这两个标记；
        缺一个，审计就会把"人没让做"和"做了但失败"混成同一类。

        ⚠️ 刻意**不**设置 last_snapshot_text：设了之后下一次 observe 会比对出
        "页面没有任何变化"，并告诉模型"上一轮动作无效"——可这一轮根本没执行动作，
        那句话是假的，只会把它推向"那我再点一次"。
        """
        reason = str(state.get("note") or "").strip()

        try:
            action = Action(**(state.get("action") or {}))
        except Exception as exc:
            # 动作还原不出来时**不假装能继续**：拒绝路径虽然不用执行它，
            # 但"拒的是什么"是唯一能告诉模型的信息，也是这条 trace 的骨架。
            # 连它都没了就说明审批行已经坏了，诚实失败比编一句"上一步被拒了"好。
            return self._stop("bad_request", note=f"审批动作无法还原：{exc}")

        context = state.get("context") or {}
        self.snapshot = await take_snapshot(self.session.page)
        # current_url 在这里补：续跑发生在 observe 之前，不补的话 trace 行的 url 是空串
        self.current_url = self.snapshot["url"]
        # 回填失败不阻断（见 docstring）：只是把情况写进说明，让模型自己判断要不要重填
        problem = await self._restore_form(context.get("form_state") or [])

        # 记一行"被用户拒绝，没有执行"。
        # _record 会把冻结的 decision 一起带上，回放器读到的就是
        # "暂停时那一问 + 模型当时的输出 + 人否决了它"——这是审计和复盘要的完整证据。
        self._record(
            action,
            ok=False,
            error=f"用户拒绝：{reason or '未填理由'}",
            started=time.perf_counter(),
        )
        self.trace[-1]["rejected"] = True
        await self._flush()

        # ① 硬约束：从这一步起**每一轮**都要把这个否决带上（理由见 __init__ 的注释①）
        hint = f"你上一步想执行「{describe(action)}」，被用户拒绝了。"
        if reason:
            hint += f"用户的理由：{reason}。"
        hint += (
            "这条要求优先于上面的任务本身：不要重复那个动作，按用户说的做；"
            "按用户说的已经能回答任务的话，直接 finish。"
        )
        self.rejection_hints.append(hint)

        # ② 黑名单：同一个动作要是再冒出来，不再问人（理由见 __init__ 的注释②）
        key = self._action_key(action)
        if key not in self.rejected_actions:
            self.rejected_actions.append(key)

        # 表单没还原全这件事只影响这一轮，属于一次性提示
        if problem:
            self.resume_notice = (
                f"另外，暂停前填的表单内容没能完整还原：{problem}。"
                "需要那些值时请按当前页面重新填。"
            )
        return ObserveEvent()

    def _action_key(self, action: Action) -> str:
        """给动作算一个跨浏览器生命周期的身份，用于"这个动作人已经拒过了"。

        为什么不能用 `describe(action)`：它带元素编号（`click [e6]`），
        而编号是 take_snapshot 现拍现打的临时属性——浏览器重开后就变了。
        拿编号当身份，"拒绝 e6"和"再点 e5（同一个按钮）"会被当成两回事。

        所以用「动作类型 + 元素文字」：`click:提交` 才是那个按钮的语义身份。
        和 _relocate_target 用的是同一套定位思路。

        ⚠️ type 动作**不**回退到 action.text：那里装的可能是密码原文，
        这个 key 会进 context（落进审批表的 JSONB），绝不能把密码抄进去。
        """
        # 先在**暂停时的快照**里找，再退到当前快照。
        # 为什么必须两份都试：拒绝路径里，action.target 是暂停那一轮的老编号（e3），
        # 而 self.snapshot 是重开浏览器后新拍的（编号变成 e5）——只查当前快照会查不到，
        # 于是 key 退化成 "click:e3"，下一次模型点 e12「提交」就对不上了，黑名单形同虚设。
        # 这和 _relocate_target 面对的是同一个事实：编号是临时的，元素文字才是身份。
        old_snapshot = (self._decision_inputs or {}).get("snapshot") or {}
        candidates = list(old_snapshot.get("elements") or []) + list(
            (self.snapshot or {}).get("elements") or []
        )
        element = next((el for el in candidates if el.get("id") == action.target), None)
        label = ((element or {}).get("text") or "").strip()
        if not label and action.action is not ActionType.TYPE:
            # 没有可读文字时退到编号：至少能挡住"同一个按钮被连拒两次"，
            # 代价是浏览器重开后可能认不出来（已知边界，漏判比误判安全）
            label = action.target or ""
        return f"{action.action.value}:{label}"

    def _rejection_text(self) -> str:
        """把"用户否决过什么"拼成一段给模型看的话。没被拒过就是空串。

        为什么保留多条而不是只留最后一条：连续两次拒绝往往针对不同动作
        （"别提交" → "也别点删除"），只留最后一条等于把前一个约束忘掉。
        但也**不能无限堆**：每多一条就多挤掉一点快照的位置，
        弱模型的注意力预算是按字符算的，所以只保留最近 3 条。
        """
        if not self.rejection_hints:
            return ""
        return " ".join(self.rejection_hints[-3:])

    def _relocate_target(self, old_id: str) -> tuple[str | None, str]:
        """把"暂停时的编号"翻译成"这次快照里的编号"。返回 (新编号或 None, 说明)。

        ⚠️ 这一步是 P5a-3 实测踩出来的必修项，不写就是"批准了但什么也没发生"：

        元素编号（e6）不是 DOM 里的稳定标识，而是 take_snapshot() **现拍现打**的
        临时属性：`[data-wp-id="e6"]`。浏览器重开、页面重新加载之后，
        页面上根本不存在这个属性 → 点击必然失败（element detached / 找不到元素）。
        实测症状极具欺骗性：审批项显示 approved、trace 里也有一条 click，
        但界面毫无变化，任务继续往下走——看起来像"闸门没起作用"。

        定位策略，按可靠性排序：
        1. **按 role + 文字匹配**（首选）。这才是"那个按钮"的语义身份：
           「button + 提交」比「e6」稳定得多。旧快照从 trace 的 decision 里取。
        2. 编号在新快照里仍然存在（DOM 没变且排序没变）→ 直接用。
        3. 都没有 → 返回 None，上层放弃执行并告知模型。

        ⚠️ 已知边界：同一页面上有两个文字完全相同的按钮时，会取第一个。
        真实做法应该是存一个稳定选择器（XPath / CSS path）而不是文字匹配，
        但那要给快照 JS 加字段、还要评估页面改版后的稳定性，P5 v0.1 不做。
        """
        new_elements = (self.snapshot or {}).get("elements") or []
        new_by_id = {el.get("id"): el for el in new_elements}

        old_snapshot = (self._decision_inputs or {}).get("snapshot") or {}
        old_el = next(
            (el for el in (old_snapshot.get("elements") or []) if el.get("id") == old_id),
            None,
        )
        if old_el is None:
            # 拿不到旧快照（历史数据、或 decision 被裁剪过）：只能赌编号没变
            if old_id in new_by_id:
                return old_id, "没有暂停时的快照，按原编号执行"
            return None, f"没有暂停时的快照，且新页面上也没有编号 {old_id}"

        role = (old_el.get("role") or "").strip()
        text = (old_el.get("text") or "").strip()
        if role and text:
            for el in new_elements:
                if (el.get("role") or "").strip() == role and (el.get("text") or "").strip() == text:
                    return el.get("id"), f"按「{role} {text}」重新定位到 {el.get('id')}"
        if old_id in new_by_id:
            return old_id, "编号在新页面里仍然存在"
        return None, f"恢复后的页面上找不到「{role} {text or old_id}」（原来编号 {old_id}）"

    # ------------------------------------------------ 视觉兜底（P6a，决策 1-C）
    async def _vision_trigger(self) -> str | None:
        """要不要看一眼图？返回触发原因（写进 trace），None 表示不看。

        三层判断，顺序就是优先级：
        0. 进程级总闸关着 / 本任务看够了 → 一律不看
           （总闸默认关是回归防护的硬要求：视觉会改 prompt，
             开着会让现有评测基线不可比，见 P6 计划第十节）
        1. 任务级强制（vision=true）→ 看，用于 canvas 演示和评测对比
        2. 连续 N 步动作失败 → 看（模型在猜，说明 DOM 这条信息通道不够用了）
        3. DOM 稀疏（可交互元素 < 阈值）**且**页面上真有 canvas/大图 → 看
           （"DOM 稀疏"单独成立不算：纯文章页元素也少，但正文够模型读，
             看一眼截图纯属浪费——这是条件 1 只在"有视觉线索"时成立的原因）
        """
        if not settings.vision_enabled:
            return None
        if self.vision_calls >= VISION_MAX_CALLS:
            return None
        if self.vision_forced:
            return "task"
        if not settings.vision_auto_fallback:
            return None
        if self.consecutive_failures >= settings.vision_trigger_max_failures:
            return "failures"
        if (self.snapshot or {}).get("elements_total", 0) < settings.vision_trigger_min_elements:
            cues = await page_visual_cues(self.session.page)
            if cues["canvas"] or cues["big_image"]:
                return "sparse_dom"
        return None

    async def _vision_understand(self) -> tuple[dict | None, str]:
        """截当前视口 → 问视觉模型 → 返回 (trace 记录, 拼进 notice 的提示)。

        两个返回值分开的理由：
        - 记录是给统计/回放用的（"这一步看没看图、看到了什么"），永远要留痕；
        - 提示是给模型的，只有真看到东西时才有内容——失败时硬塞一句
          "看起来像加载中"反而会让模型编故事。

        ⚠️ 契约：截图必须和 self.snapshot 是同一帧（上面刚拍完马上截）。
        不是同一帧的话，模型说的"左上角按钮"和元素编号就对不上号了。
        """
        trigger = await self._vision_trigger()
        if trigger is None:
            return None, ""

        started = time.perf_counter()
        try:
            image = await self.session.page.screenshot(type="png")
        except Exception as exc:  # noqa: BLE001 —— 截图失败不该搞挂任务
            return (
                {
                    "used": False,
                    "model": settings.vision_model,
                    "trigger": trigger,
                    "ms": round((time.perf_counter() - started) * 1000),
                    "note": f"截图失败：{type(exc).__name__}",
                },
                "",
            )

        self.vision_calls += 1
        summary = await vision.describe(image)
        ms = round((time.perf_counter() - started) * 1000)
        record: dict = {
            "used": summary is not None,
            "model": settings.vision_model,
            "trigger": trigger,
            "ms": ms,
        }
        if summary is None:
            # 模型没给出内容（限流/超时/空回复）：留痕但不污染 prompt
            return record, ""

        record["summary"] = summary[:300]
        # 提示的措辞有两个刻意的选择：
        # ① "可能不完整"——模型只看图，读不到 DOM 里的隐藏信息，别让它以为这是全文；
        # ② "动作仍必须用元素编号"——把结论钉死在"参考"的位置上（决策 2-A），
        #    否则模型会尝试编一个不存在的元素去点 canvas 里的东西。
        notice = (
            f"视觉参考（截图看到的，可能不完整）：『{summary[:400]}』"
            "——这只是参考，动作仍必须用元素编号表达。"
        )
        return record, notice

    @step
    async def step_observe(self, ctx: Context, ev: ObserveEvent) -> DecideEvent | StopEvent:
        """看一眼页面。步数上限在这里判——因为这是每一轮的唯一入口。"""
        if self.step_no >= self.max_steps:
            return self._stop("step_limit", note=f"已走满 {self.max_steps} 步仍未完成")

        self.step_no += 1
        self.snapshot = await take_snapshot(self.session.page)

        # 空白页重试。
        # 真实站点实测（R3）：点进作者页后拍到的是**纯白画面**——元素 0、正文 0，
        # 截图整张白。原因是换页瞬间旧文档已清空、新文档还没画出来。
        # 我们当时没有任何检测，把这张白纸喂给模型，它就只能编元素编号。
        if _is_blank(self.snapshot):
            for _ in range(EMPTY_RETRY - 1):
                await self.session.settle(EMPTY_WAIT_MS)
                self.snapshot = await take_snapshot(self.session.page)
                if not _is_blank(self.snapshot):
                    break

        self.current_url = self.snapshot["url"]
        text = render_snapshot_text(self.snapshot)

        # 页面变化要主动汇报给模型，两种都有用：
        # - 完全没变 → 这个动作白做了，别重复
        # - 变了 → 变在哪，结果是不是已经出现（"提交成功：…"这种）
        notice = ""
        if _is_blank(self.snapshot):
            # 重试完仍是白纸：必须明说，并给一条能走的路。
            # 只说"页面为空"不够——弱模型会继续猜一个编号，然后被判非法。
            notice = (
                "当前页面是空白的：没有任何可交互元素，也没有正文。"
                "这通常意味着页面还在加载。请用 scroll 触发加载，"
                "绝对不要猜元素编号（页面上不存在任何编号）。"
            )
        elif self.last_snapshot_text:
            jumped = bool(self.last_url) and self.last_url != self.snapshot["url"]
            if jumped:
                # 跳页后**不报 diff**：整页都是新的，diff 出来的都是半截碎片
                # （P7-D1 实测原样是『2 （第 2 页） 机械键盘 上一页 ｜ 29 4 上一 tail 返回首 in x』）。
                # 模型读这种碎片读不出"我换了页面"，更读不出"编号全重排了"，于是沿用旧编号乱点。
                # 报地址变化 + 明确要求重新核对元素文字，才是这一步真正需要传达的信息。
                notice = (
                    f"页面已跳转到 {self.snapshot['url']}，元素编号全部重新分配过，"
                    "请按文字核对编号再用。"
                )
            elif text == self.last_snapshot_text:
                # ⚠️ 措辞的教训（P7-D1 T8 实测）：这句话原本写的是"说明这个动作无效"。
                # 但系统只能观察到"页面文字没变"，**没有资格**断定动作无效——
                # 搜一个不存在的关键词时，动作生效了，结果就是"共 0 条结果"，
                # 页面显示当然不变。原措辞把模型推向"那是没点上，我再点一次"，
                # 它连点 3 次搜索按钮直到被判 loop。
                # 所以这里只说"没观察到变化"，并把"可能就是正确结果"摆在两条出路里。
                notice = (
                    "页面内容没有变化。可能是动作没生效，也可能是它生效了、"
                    "但这页面的正常反应就是不改变显示（比如搜索结果是空的）。"
                    "当前页面信息够回答任务就直接 finish，否则换个动作，别重复刚才那个。"
                )
            else:
                added = _text_delta(self.last_snapshot_text, text)
                if added:
                    notice = f"页面已更新，新出现的内容：『{added}』"

        # 白纸状态不更新"上一轮快照"：
        # 拿它当基准的话，接下来每一次比较都会把整页算成"新增"，提示全是噪音。
        # 地址同理：白纸时地址可能还没落到最终值，拿它当基准会误报一次"跳转"。
        if not _is_blank(self.snapshot):
            self.last_snapshot_text = text
            self.last_url = self.snapshot["url"]

        # 动作回显放最前面：它是"你刚做了什么"的直接答案，比"页面变了什么"更要紧。
        # 只出现一轮，拼完就清空——它不是常驻约束，重复刷只会变成噪音。
        if self.action_echo:
            notice = f"{self.action_echo} {notice}".strip()
            self.action_echo = ""

        # P6 视觉兜底：DOM 稀疏 / 连续失败 / 任务级强制时，让视觉模型看一眼截图，
        # 结论并进 notice（[观察] 通道，不新增 prompt 段落——见决策 2-A）。
        # 位置放在"页面变化提示"之后、"续跑说明"之前：
        # 它是**新的观察信息**，不是一次性的流程说明，两者不该抢同一个位置。
        vision_record, vision_note = await self._vision_understand()
        if vision_record is not None:
            self._vision_record = vision_record
        if vision_note:
            notice = f"{notice} {vision_note}".strip()

        # 续跑时"上一轮动作没执行/执行失败"的说明要并进来（P5a-3）。
        # 放在最后、只拼一次：它是**一次性**信息，不能每轮都重复告诉模型。
        if self.resume_notice:
            notice = f"{self.resume_notice} {notice}".strip()
            self.resume_notice = ""

        return DecideEvent(snapshot_text=text, notice=notice)

    @step
    async def step_decide(
        self, ctx: Context, ev: DecideEvent
    ) -> GateEvent | DecideEvent | StopEvent:
        """问模型 + 校验。校验不过就带着错误原因再问一次（最多一次）。"""
        elements = {el["id"]: el for el in (self.snapshot or {}).get("elements", [])}

        # ⚠️ 冻结必须发生在 build_user_prompt 之前，原因有两个：
        # 1. 下面的 _absorb_plan 会改写 self.plan / self.progress——冻结晚了，
        #    回放时拿到的就是"被吸收后"的进度，跟模型当时看到的不一样；
        # 2. ev.snapshot_text 是这次实际喂给模型的字符串，事后用 render_snapshot_text
        #    重渲染可能因渲染函数变更而失真——冻结的是"原始输入"本身。
        # 被拒绝后回环重试会重新走到这里，error_hint 会被更新成最新的，正好。
        self._decision_inputs = {
            "snapshot_text": ev.snapshot_text,
            "snapshot": self.snapshot,
            "plan": list(self.plan),
            "progress": list(self.progress),
            "notice": ev.notice,
            "error_hint": ev.error_hint,
            "max_steps": self.max_steps,
            # 用户否决过什么（P5a-4）。一起冻结，回放器才能重建出**同一份** prompt——
            # 不冻结的话，重放一条"被拒过"的轨迹时重放出来的 prompt 里缺那一段，
            # 对比结论就是在拿两个不同的问题互相打分。
            "rejection": self._rejection_text(),
        }

        user_prompt = build_user_prompt(
            goal=self.goal,
            step=self.step_no,
            max_steps=self.max_steps,
            snapshot_text=ev.snapshot_text,
            history=self.trace,
            error_hint=ev.error_hint,
            notice=ev.notice,
            plan=self.plan,
            progress=self.progress,
            rejection=self._rejection_text(),
        )

        payload: dict | None = None
        try:
            payload = await self.llm.decide(SYSTEM_PROMPT, user_prompt)
            # 留住模型这次给的原始输出：回放对比时"老决策"就是它
            self._last_payload = payload
            # 先吸收 plan/progress，再校验动作：plan 不合法不该让整个动作作废
            self._absorb_plan(payload)
            action = check_action(
                parse_raw(payload),
                elements=elements,
                allowed_hosts=settings.allowed_hosts,
            )
        except (ActionError, LLMFormatError) as exc:
            self.invalid_actions += 1
            # P0：被拒绝的决策也必须留痕。
            # 之前这里直接 return，导致失败任务的 trace 是**空数组**——
            # R4 就是"steps=1、trace=0 条"，完全不知道模型当时看到了什么、输出了什么。
            self._record_rejected(str(exc), payload)
            await self._flush()
            if self.invalid_actions >= MAX_INVALID_ACTIONS:
                return self._stop("invalid_action", note=str(exc))
            # 回到自己这个 step，把错误原因带上去重新问
            return DecideEvent(
                snapshot_text=ev.snapshot_text, error_hint=str(exc), notice=ev.notice
            )

        return GateEvent(action=action)

    @step
    async def step_gate(
        self, ctx: Context, ev: GateEvent
    ) -> ActEvent | StopEvent:
        """审批闸门：高风险动作在这里停下来问人（P5a）。

        它在循环里的位置是刻意选的——decide 之后、act 之前：
        这是唯一"模型已经想好、但还没产生任何副作用"的时刻。
        放到 act 之后就只能是事后报警，什么都挽回不了。

        停下来的做法（关键设计，见 P4/P5 实施计划）：
        不在进程里挂起等审批。把"待审批动作 + 决策上下文"通过 on_approval 交给调用方落库，
        然后返回 StopEvent —— 任务这一趟就正常结束了，worker 槽位立刻释放。
        用户批准后由 API 重新入队一个 resume job，重开浏览器接着跑。
        为什么必须这样：worker 会重启、job 有 240s 超时，在内存里等的任务迟早丢。

        返回 StopEvent 时 status 用 waiting_approval：
        它既不是成功也不是失败，是"活着但没人在推"。统计口径要单独处理（见 app/status.py）。
        """
        # 开关关闭 → 直接放行。两种关闭方式：
        # ① 进程级 APPROVAL_ENABLED=false（紧急止血 / 整批压测）
        # ② 任务级 require_approval=false（eval_real 等批处理脚本提交时声明）
        # 两者取"与"，见 __init__ 里的说明。
        if not self.approval_enabled:
            return ActEvent(action=ev.action)

        # 人已经明确拒过这个动作，模型还坚持要执行 → 不再问人，直接收尾（P5a-4）。
        # 为什么不像刚才那样再停一次等人批：同一个问题连问两遍，人只会觉得
        # "它根本没听我说话"；而且这条路实测能无限循环（拒一次、换个步号再试一次，
        # 5 次都不停），等于给"审批乒乓"开了一个没有出口的入口。
        # 用 loop 这个归因标签：模型确实在重复同一个动作，只是这次是被拦下的那种重复。
        key = self._action_key(ev.action)
        if key in self.rejected_actions:
            return self._stop(
                "loop",
                note=f"用户已经拒绝过「{describe(ev.action)}」，模型仍坚持要执行同一个动作，已停止",
            )

        reason = approval_gate(ev.action, self.snapshot)
        if reason is None:
            return ActEvent(action=ev.action)

        # 命中：把审批信息交给调用方落库（P5a-1 先写进 meta，P5a-2 再落表）
        self.pending_approval = {
            "step_no": self.step_no,
            "reason": reason,
            "action": ev.action.model_dump(),
            "snapshot_url": self.current_url,
            # 恢复时要用到的全部决策上下文。为什么存 current_url 而不存整页：
            # 浏览器活体不可序列化，恢复只能重开浏览器 + goto 这个地址重新观察。
            "context": {
                "goal": self.goal,
                "plan": list(self.plan),
                "progress": list(self.progress),
                "current_url": self.current_url,
                "step_no": self.step_no,
                # ⚠️ 光有地址还不够（P5a-3 实测踩出来的）：goto 出来的是**一张刚打开的
                # 页面**，暂停前填的表单全空。批准"提交"就变成了提交一张空表。
                # 所以把"页面上每个能填的东西现在装了什么"一起存下来，恢复时先回填。
                "form_state": self._capture_form_state(),
                # 之前被人否决过什么（P5a-4）。必须跟着 context 走：
                # 续跑要重开浏览器、可能换一个 worker 进程，"人已经说过不行"这件事
                # 不能因此失效，否则模型可以靠"再停一次"把被拒过的动作重新提上来。
                "rejection_hints": list(self.rejection_hints),
                "rejected_actions": list(self.rejected_actions),
            },
        }
        # 密码脱敏（P5b-②）：审批卡片、approvals 表、meta.json 都会原样落这份
        # dict，明文密码不能进其中任何一个。打码后批准续跑就没法从库里拿到
        # 真值了——而批准路径是**直接重放**这个 action（"批的就是它，不再问模型"），
        # 所以真值加密成 _secret 随行（Fernet，密钥和凭证 vault 同源），
        # 续跑时解密还原。库里的最终形态：text="***" + _secret=密文，无明文。
        secret = self._current_secret(ev.action)
        if secret:
            self.pending_approval = self._redact(self.pending_approval, secret)
            self.pending_approval["action"]["_secret"] = vault.encrypt_text(secret)
        if self.on_approval is not None:
            await self.on_approval(self.pending_approval)

        # 轨迹里补一条记录：走标准 _record 保证字段齐全（url / duration_ms /
        # elements_total / decision 都在，前端和 M5a 回放器读到的是同一个 shape），
        # 再加一个 pending_approval 标记表示"这一步没有真的执行"。
        # 为什么不手写一行 append：手写准漏字段，两处 shape 一旦不一致，
        # 回放器就会在"暂停步"上读到 None 然后莫名其妙地崩。
        self._record(ev.action, ok=True, error=None, started=time.perf_counter())
        self.trace[-1]["pending_approval"] = True
        await self._flush()

        return self._stop("waiting_approval", note=reason)

    @step
    async def step_act(self, ctx: Context, ev: ActEvent) -> ObserveEvent | StopEvent:
        """执行动作。执行失败不重试旧编号，而是重新快照——页面可能已经变了。"""
        action = ev.action
        await self._apply(action)

        if action.action is ActionType.FINISH:
            return self._stop("success")

        if self._is_looping():
            return self._stop("loop", note=f"连续 {LOOP_THRESHOLD} 次相同动作：{describe(action)}")

        return ObserveEvent()

    async def _apply(
        self, action: Action, *, approved: bool = False
    ) -> tuple[bool, str | None]:
        """执行一个动作：执行 → 截图 → 记 trace → 落盘。返回 (ok, error)。

        为什么抽出来（P5a-3 加的）：续跑要执行的"被批准的那个动作"，
        这段逻辑和正常执行**一字不差**。各写一份的话，截图命名、trace 字段、
        耗时统计迟早改歪一边，于是同一步在"批准路径"和"正常路径"落下的证据
        形状不一样——回放器拿两份不同形状的轨迹做对比，结论就没有意义了。

        只负责"做"，不负责判循环/判结束：那是调用方的事。
        用户批准的动作**不判循环**——人已经明确要求执行，这时候机器再说
        "你好像在打转"就等于推翻人的决定。

        approved=True 时在 trace 行上盖一个审计标记：这一行是"人批准后才执行的"，
        不是模型自己跑出来的。事后要回答"哪些高风险动作被人放行了"，靠的就是它。
        """
        started = time.perf_counter()
        ok, error = True, None

        try:
            if action.action is ActionType.FINISH:
                self.answer = action.answer or ""
            elif action.action is ActionType.GOTO:
                assert action.url
                await self.session.goto(action.url)
            elif action.action is ActionType.CLICK:
                assert action.target
                await self.session.click(selector_for(action.target))
            elif action.action is ActionType.TYPE:
                assert action.target and action.text is not None
                await self.session.type_text(selector_for(action.target), action.text)
            elif action.action is ActionType.SELECT:
                assert action.target and action.option
                await self.session.select_option(selector_for(action.target), action.option)
            elif action.action is ActionType.SCROLL:
                await self.session.scroll(action.direction)
        except Exception as exc:
            # 动作失败（元素消失、超时、被遮挡）→ 记下来，下一轮重新拍快照
            ok, error = False, f"{type(exc).__name__}: {exc}"

        # 视觉兜底条件 2 的计数：连续失败说明模型在猜，下一步该让它看一眼图了。
        # 成功就清零——「连续」指的是中间没有一次成功，不是累计失败数。
        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1

        # 每步留一张截图：回放时"这步执行完页面长什么样"是最有说服力的证据。
        # 截图失败不算任务失败——它是辅助产物，不能反过来搞挂主流程。
        # FINISH 不截：它没改变页面，截出来是上一张的重复图。
        shot_name = None
        if self.shot_dir is not None and action.action is not ActionType.FINISH:
            shot_name = f"step_{self.step_no}.png"
            try:
                await self.session.screenshot_to(self.shot_dir / shot_name)
            except Exception:
                shot_name = None

        self._record(action, ok=ok, error=error, started=started, shot=shot_name)
        if approved:
            self.trace[-1]["approved"] = True

        # 动作回显（见 __init__ 里 action_echo 的说明）。
        # 只对"指向某个元素"的动作做：goto/scroll/finish 没有目标元素，没什么可回显的。
        # 注意取的是 self.snapshot——它是**动作执行前**的那一份，正好是模型当时看到并点下去的元素，
        # 也正是我们要让它复核的那一个。
        if action.action in (ActionType.CLICK, ActionType.TYPE, ActionType.SELECT) and action.target:
            label = self._target_label(action.target)
            if label:
                # 措辞刻意只报事实、不带训话。
                # 第一版写的是"如果你打算做的不是它，说明编号用错了——请按当前快照重新核对…"，
                # 60 多字的说教每步刷一遍，弱模型被压得不会读正文了（T5 实测：翻到第二页后
                # 直接答"当前页面没有商品信息"，而价格就写在快照里）。事实比道理管用。
                self.action_echo = f"刚才点/填的是 {action.target}『{label}』。"

        # 这条 flush 不能省。
        # 走了 FINISH 就直接 return，下面那个统一的 flush 到不了 ——
        # 后果是最后一行（finish）永远不进 on_step：JSON 里因为最后有一次全量写
        # 还看得出来，但按增量落库的 DB 会少一行，两边对不上。
        await self._flush()
        return ok, error

    # ---------------------------------------------------------------- helper
    def _target_label(self, target: str) -> str:
        """把元素编号翻成"它到底是什么"（给动作回显用）。

        返回形如 `上一页（link）`；查不到就返回空串——编号对不上只说明
        快照和动作之间状态变了，不该因此把任务判失败。
        输入框优先用 placeholder/value：真实站点上很多 input 的可见文字就是空的，
        只回显"（input）"对模型没有信息量。
        """
        for el in (self.snapshot or {}).get("elements") or []:
            if el.get("id") != target:
                continue
            role = (el.get("role") or "").strip()
            text = (el.get("text") or "").strip()
            if not text and role in ("input", "textarea"):
                text = (el.get("value") or "").strip()
            if not text and role == "input":
                text = "(空输入框)"
            if not text:
                text = "(无文字)"
            return f"{text}（{role or '元素'}）" if role else text
        return ""

    def _capture_form_state(self) -> list[dict]:
        """把"页面上每个能填的东西现在装了什么"存成一份清单（P5a-3 方案 D）。

        为什么必须有这一步：
        `snapshot_url` 只能回答"回到哪一页"，回答不了"回到什么状态"。
        goto 出来的永远是**一张刚打开的页面**——暂停前填好的姓名、邮箱、下拉选项
        全都不在了。实测最坏的情况是：人批准了「提交」，续跑后在空表单上执行了它，
        页面回显"提交成功：姓名=，邮箱=，城市=未填"。
        **动作成功（ok=True）但做的是错事**，这比失败严重得多。

        存什么、不存什么：
        - input（除密码）→ 当前 value
        - select → 当前选中项**文字**（和 select_option 用 label 保持一致）
        - checkbox / radio → 是否勾选
        - **password → 只标记"这个框有值、但恢复不了"**。真实值在快照里本来就是 `***`，
          而且明文密码绝不进审批表——那是数据泄露。代价是含密码的任务续跑会被拒绝，
          这个取舍是对的：宁可不动，也不能带着空密码去提交。

        只留有内容的字段（checkbox 除外）：一屏空字段既撑大 context，
        也让后面的核对噪音很大。
        """
        fields: list[dict] = []
        for el in (self.snapshot or {}).get("elements") or []:
            role = (el.get("role") or "").strip()
            text = (el.get("text") or "").strip()

            if role in ("input", "textarea"):
                value = el.get("value") or ""
                input_type = (el.get("input_type") or "text").strip()
                if input_type == "password":
                    # 有值才记：空密码框不用管
                    if value:
                        fields.append(
                            {
                                "role": role,
                                "text": text,
                                "value": "",
                                "restorable": False,
                                "reason": "密码字段的真实值从不落库",
                            }
                        )
                    continue
                if value:
                    fields.append(
                        {"role": role, "text": text, "value": value, "restorable": True}
                    )
            elif role == "select":
                selected = (el.get("selected_text") or "").strip()
                if selected:
                    fields.append(
                        {"role": role, "text": text, "value": selected, "restorable": True}
                    )
            elif role in ("checkbox", "radio"):
                # 勾选状态本身就是"内容"，不管勾没勾都要记（要还原"没勾"这个事实）
                fields.append(
                    {
                        "role": role,
                        "text": text,
                        "value": bool(el.get("checked")),
                        "restorable": True,
                    }
                )
        return fields

    def _find_by_role_text(self, role: str, text: str) -> dict | None:
        """在**当前快照**里按「角色 + 文字」找一个元素。

        这是跨浏览器生命周期的定位方式。编号（e6）只在当轮快照里有效，
        而「button + 提交」才是那个元素的语义身份。
        """
        for el in (self.snapshot or {}).get("elements") or []:
            if (el.get("role") or "").strip() == role and (el.get("text") or "").strip() == text:
                return el
        return None

    async def _restore_form(self, form_state: list[dict]) -> str:
        """把暂停前的表单内容填回去，并核对结果。返回 "" 表示还原成功。

        为什么失败了就返回说明、而不是硬着头皮继续：
        后续那个"被批准的动作"是作用在**表单内容**上的（点提交=提交这些值）。
        内容不对的时候执行它，等于让人批准的东西变成另一件事。
        宁可什么都不做，把"现场还原不了"如实告诉模型，让它重新判断。

        核对为什么不能省：fill / select_option 都是"发出去就算成功"的调用，
        页面自带的 onchange 可能把值改掉、也可能字段被 JS 重置。
        **动作成功 ≠ 做对了**，只有回读一遍才算数。
        """
        if not form_state:
            return ""

        # 敏感字段先拦：它们永远恢复不了，留着必然导致核对失败，
        # 不如直接说清楚"因为密码不能存，所以这次续跑做不了"。
        blocked = [f for f in form_state if not f.get("restorable")]
        if blocked:
            names = "、".join((f.get("text") or "密码框") for f in blocked)
            return f"无法还原敏感字段（{names}）：密码的真实值从不落库"

        for field in form_state:
            el = self._find_by_role_text(field["role"], field["text"])
            if el is None:
                return f"恢复失败：页面上找不到「{field['text']}」这个字段"
            try:
                if field["role"] == "select":
                    await self.session.select_option(
                        selector_for(el["id"]), str(field["value"])
                    )
                elif field["role"] in ("checkbox", "radio"):
                    # 只有状态不符才点：否则会把已经勾上的点掉
                    if bool(el.get("checked")) != bool(field["value"]):
                        await self.session.click(selector_for(el["id"]))
                else:
                    await self.session.type_text(
                        selector_for(el["id"]), str(field["value"])
                    )
            except Exception as exc:
                return f"恢复失败：填写「{field['text']}」时出错（{type(exc).__name__}: {exc}）"

        # ---- 核对：重新拍一次，逐字段回读 ----
        self.snapshot = await take_snapshot(self.session.page)
        for field in form_state:
            el = self._find_by_role_text(field["role"], field["text"])
            if el is None:
                return f"核对失败：「{field['text']}」在回填后消失了"
            if field["role"] in ("checkbox", "radio"):
                if bool(el.get("checked")) != bool(field["value"]):
                    return f"核对失败：「{field['text']}」的勾选状态没还原"
            elif field["role"] == "select":
                actual = (el.get("selected_text") or "").strip()
                if actual != field["value"]:
                    return f"核对失败：「{field['text']}」选中的是 {actual!r}，应为 {field['value']!r}"
            else:
                actual = el.get("value") or ""
                if actual != str(field["value"]):
                    return f"核对失败：「{field['text']}」的值是 {actual!r}，应为 {field['value']!r}"
        return ""

    async def _skip_approved(self, action: Action, reason: str) -> ObserveEvent:
        """放弃执行"被批准的动作"，并把原因如实告诉模型。

        三种情况走这里：页面还原不了、元素找不到、表单内容核对不过。
        共同点是"现在执行它就是在做另一件事"，所以宁可不做。

        为什么要留一条 trace 行（ok=False + approved=True + resume_skipped）：
        审计要能回答"人批了，为什么没执行"。三个标记缺一不可——
        没有 approved 就看不出它和审批有关，没有 resume_skipped 就分不清
        是"执行失败"还是"根本没试"。
        """
        self._record(action, ok=False, error=reason, started=time.perf_counter())
        self.trace[-1]["approved"] = True
        self.trace[-1]["resume_skipped"] = True
        await self._flush()
        # describe 里的密码就地抹掉：notice 会冻结进下一轮 decision 落盘，
        # 下轮 _record 只按"下一轮动作"判定脱敏——下轮动作换成了 goto 就漏了。
        described = describe(action)
        if action.action is ActionType.TYPE and action.text:
            described = described.replace(action.text, "***")
        self.resume_notice = (
            f"上一次被批准的动作（{described}）没有执行：{reason}。"
            "恢复后的页面已经和暂停时不同，请按当前页面重新判断该做什么。"
        )
        return ObserveEvent()

    async def _flush(self) -> None:
        """把当前 trace 交给调用方落盘。

        单独抽出来的理由：现在有两条路径要落盘（正常执行完一步、决策被拒绝），
        以前只有前者，失败路径忘了写，结果失败任务的 trace 是空数组。
        落盘失败不能影响任务本身——trace 是证据，不是主线。
        """
        if self.on_step is None:
            return
        try:
            await self.on_step(self.trace)
        except Exception:
            pass

    def _current_secret(self, action: Action) -> str | None:
        """这个动作正在往密码框里填内容吗？是 → 返回要填的明文，不是 → None。

        服务于 trace 脱敏：只有"确实往密码框填"的动作才需要把 text 从轨迹里抹掉。
        不能拿任意动作的 text 去全轨迹替换——正常输入（搜索词、用户名）会
        被误伤成 ***，回放和审计就废了。

        判定调 approval.is_password_input，和闸门共用同一份"什么是密码框"的
        标准：将来 RISKY_INPUT_TYPES 扩充时，"拦的"和"藏的"自动保持一致。
        """
        if action.action is ActionType.TYPE and is_password_input(self.snapshot, action.target):
            return action.text
        return None

    @staticmethod
    def _redact(obj, secret: str):
        """递归把 obj 里出现的 secret 换成 ***，返回清洗后的副本（不改原对象）。

        两种命中方式都要处理：
        - 值**恰好等于** secret：payload 的 text 字段（"pass1234"）→ 整值变 ***
        - 值**包含** secret：describe 产出的 "type e5 <- 'pass1234'"、
          thought 里写着"我要输入 pass1234"、resume 失败说明引用了动作描述
          → 只替换那一段，句子其余部分保留

        为什么在 _record 组装完**整行**清洗、而不是各字段单独处理：
        泄漏点是"任何可能装下动作文本的地方"——action 描述、thought、
        decision.payload、decision 里的 notice……逐字段列举迟早漏一个
        （这次就漏了 decision）。整行递归一次，将来加新字段自动被覆盖。

        为什么返回新副本而不是原地改：decision 里的 payload 和 self._last_payload
        是**同一个对象**（_decision_blob 直接放引用），原地改会把内存里模型
        的原始输出也改掉——那一层只属于落盘，不该影响运行时状态。
        """
        if isinstance(obj, dict):
            return {k: BrowserAgentWorkflow._redact(v, secret) for k, v in obj.items()}
        if isinstance(obj, list):
            return [BrowserAgentWorkflow._redact(v, secret) for v in obj]
        if isinstance(obj, str):
            if obj == secret:
                return "***"
            if secret and secret in obj:
                return obj.replace(secret, "***")
        return obj

    def _decision_blob(self, payload: dict | None = None) -> dict:
        """组装"这一步的完整决策上下文"，写进 trace 供回放器使用。

        设计原则：**零转换、零信息损失**。
        - snapshot 整个 PageSnapshot 原样塞进去（含 elements / url / title / 截断标记），
          回放时 check_action 要用它校验编号合法性，裁剪任何字段都可能让结果失真；
        - snapshot_text 用冻结在 _decision_inputs 里的那份——它是当时真实喂给模型的字符串；
        - payload 不传时用 _last_payload（决策通过路径），被拒路径显式传原始输出。

        体积代价：一条 decision 约几 KB～几十 KB。这是"历史即模拟器"的地基，
        没有它，M5a 的所有对比都只能靠脑补。
        """
        inputs = self._decision_inputs or {}
        return {
            "snapshot_text": inputs.get("snapshot_text", ""),
            # PageSnapshot 是 TypedDict（本质是 dict），json 序列化无损
            "snapshot": inputs.get("snapshot"),
            "payload": payload if payload is not None else self._last_payload,
            "plan": list(inputs.get("plan") or []),
            "progress": list(inputs.get("progress") or []),
            "notice": inputs.get("notice", ""),
            "error_hint": inputs.get("error_hint", ""),
            # P5a-4：用户否决段落。回放器必须把它一起还原，否则重放一条
            # "被拒过"的轨迹时问出去的问题是少了一段的——两个不同的问题不能用来对比。
            # 老条目没有这个键，默认空串 → 老基线不受影响。
            "rejection": inputs.get("rejection", ""),
            "max_steps": inputs.get("max_steps", 0),
        }

    def _record_rejected(self, error: str, payload: dict | None) -> None:
        """记一次"决策被拒绝"。

        为什么要记**模型的原始输出**，而不只记错误原因：
        错误原因只说"e24 是 link，不能输入文字"，但看不出模型想干什么。
        把原始 JSON 留下来，才能区分是"它选错了元素"还是"它误解了页面结构"——
        这两种要修的地方完全不同。

        副作用：trace 里会出现 ok=False 但没执行任何动作的条目。
        `step` 号会和下一轮重试相同（重试不推进步数），这是有意的——
        它们属于同一步的两个尝试。
        """
        thought = ""
        raw = "(模型没有返回可解析的 JSON 对象)"
        # 被拒路径也要脱敏：模型"想往密码框填内容但动作被判非法"时，
        # 原始 payload 里照样带着明文。此时动作还没 parse 成 Action，
        # 只能从扁平 payload 里拿 target 判定（parse 失败 → payload 不是 dict
        # → 根本不存在结构化的 text，无从泄漏）。清洗必须放在取 thought /
        # dumps raw **之前**——顺序反了，明文已经进了字符串再洗就晚了。
        secret = None
        if isinstance(payload, dict) and is_password_input(self.snapshot, payload.get("target")):
            secret = str(payload.get("text") or "")
            if secret:
                payload = self._redact(payload, secret)
        if isinstance(payload, dict):
            thought = str(payload.get("thought") or "")
            raw = json.dumps(payload, ensure_ascii=False)

        row = {
                "step": self.step_no,
                "url": self.current_url,
                "thought": thought,
                # 前缀标明"这是被拒绝的"，前端时间线一眼能看出它没执行
                "action": f"[被拒绝] {raw[:300]}",
                "ok": False,
                "error": error,
                "duration_ms": 0,
                "elements_total": (self.snapshot or {}).get("elements_total", 0),
                "screenshot": None,
                "llm_usage": dict(self.llm.last_usage),
                # 被拒的决策也回放：它往往是 prompt 问题最集中的样本
                #（模型读了什么、输出了什么、为什么被判非法）
                "decision": self._decision_blob(payload),
            }
        # 视觉留痕也要带上：被拒的这一步同样"看过图"，
        # 统计兜底率时漏掉这些行，会把"视觉没帮上忙"的案例算成"没用视觉"。
        if self._vision_record is not None:
            row["vision"] = self._vision_record
            self._vision_record = None
        self.trace.append(row)

    def _record_nav_failure(self, url: str, error: str) -> None:
        """起始页就打不开时也留一条痕。

        step 用 0 表示"还没做任何决策"。以前这种情况 trace 是空的，
        报告里只能看到"状态=nav_failed、步数=0"，看不到它想打开哪个地址、
        失败在 DNS 还是超时——而这两者的修法完全不同。
        """
        self.trace.append(
            {
                "step": 0,
                "url": url,
                "thought": "",
                "action": "[打开起始页]",
                "ok": False,
                "error": error,
                "duration_ms": 0,
                "elements_total": 0,
                "screenshot": None,
                "llm_usage": {},
            }
        )

    def _absorb_plan(self, payload: dict) -> None:
        """吸收模型给的 plan / progress。

        宽容处理：模型经常给不规范的东西（plan 是字符串、progress 是 ["1"]），
        这些不该让动作作废——plan 只是辅助它自己记住进度，错了顶多是退化回原样。
        """
        raw_plan = payload.get("plan")
        if isinstance(raw_plan, list) and not self.plan:
            self.plan = [str(item)[:80] for item in raw_plan[:5] if str(item).strip()]

        raw_progress = payload.get("progress")
        # 没有 plan 时 progress 无处挂靠：既渲染不出来，也没法判断越界，直接忽略
        if not isinstance(raw_progress, list) or not self.plan:
            return

        numbers: list[int] = []
        for value in raw_progress:
            try:
                numbers.append(int(value))
            except (TypeError, ValueError):
                continue

        # 这轮没给出任何有效序号 → 保留上一轮的，绝不把"已完成"清空。
        # 模型偶尔会漏字段、也可能给 []；清空等于告诉它进度倒退了，它会从头再走一遍。
        if not numbers:
            return

        # 越界序号丢掉：progress 只能指向 plan 里真实存在的条目。
        # 旧写法是 limit = max(len(plan), max(numbers))，等于允许"9 号"挂在只有 3 条的计划上，
        # 渲染时它对应不到任何一行 → 模型同时看到"还剩 3 条未完成"和 progress=[9]，自相矛盾。
        self.progress = sorted({n for n in numbers if 1 <= n <= len(self.plan)})

    def _record(
        self,
        action: Action,
        *,
        ok: bool,
        error: str | None,
        started: float,
        shot: str | None = None,
    ) -> None:
        """写一条 trace。字段是给 M5 前端和 M6 归因用的，少一个都影响排查。"""
        row = {
            "step": self.step_no,
            "url": self.current_url,
            "thought": action.thought,
            "action": describe(action),
            "ok": ok,
            "error": error,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "elements_total": (self.snapshot or {}).get("elements_total", 0),
            "screenshot": shot,
            "llm_usage": dict(self.llm.last_usage),
            # M5a：冻结的决策输入，回放器靠它重建"当时这一问"
            "decision": self._decision_blob(),
        }
        # 视觉兜底留痕（P6a）：这一步看没看图、什么触发、看到了什么。
        # 放在打码**之前**挂上去——summary 是模型对截图的复述，理论上可能
        # 读到页面上不该落盘的内容（比如测试站当年印在页面上的密码），
        # 而下面的 _redact 会连它一起洗。
        # 挂完立刻清空：一条视觉记录只属于它被触发的那一步。
        if self._vision_record is not None:
            row["vision"] = self._vision_record
            self._vision_record = None
        # 密码脱敏：这一步在往密码框填内容时，整行（含 decision 里的原始
        # payload / 模型 thought / 决策说明）清洗后再落 trace。
        # 必须在**源头**打码而不是靠展示层过滤：trace 一旦 append 就会同时流去
        # 三个地方——落盘 trace.json、实时 prompt 的 [已执行] 段、M5a 回放器。
        # 源头干净则三处自洽；靠下游各自过滤，漏一处就是明文落库。
        # `or self._resume_secret`：续跑批准的密码动作在暂停步上已经判定过一次，
        # 这里再判一次是因为正常执行路径也会经过本函数。
        secret = self._current_secret(action) or self._resume_secret
        if secret:
            row = self._redact(row, secret)
        self.trace.append(row)

    def _is_looping(self) -> bool:
        """判断是不是在原地打转。两种形态都要抓：

        1. 同一个动作连做 3 次（连点"筛选"）——最简单，也最常见
        2. 两个动作来回摆：A B A B（"下一页"→"上一页"→"下一页"→"上一页"）
           只查"连续相同"抓不到这种，T5 实测就是在这里走满 12 步的。

        忽略 thought：它每次都会重写，拿它比对等于永远判不出循环。
        """
        actions = [item["action"] for item in self.trace]
        if len(actions) >= LOOP_THRESHOLD and len(set(actions[-LOOP_THRESHOLD:])) == 1:
            return True

        # 交替循环：最近 4 步满足 A B A B
        if len(actions) >= 4:
            last4 = actions[-4:]
            if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
                return True
        return False

    def _stop(self, status: str, note: str = "") -> StopEvent:
        """统一的结束出口。status 本身就是失败归因标签，M6 直接按它统计。"""
        result = {
            "status": status,
            "answer": self.answer,
            "steps": self.step_no,
            "note": note,
            "trace": self.trace,
            # 带上任务分解：事后能回答"到底是没有 plan，还是有 plan 但没遵守"
            "plan": list(self.plan),
            "progress": list(self.progress),
        }
        # 只有闸门停下来时才带这个键。调用方据此知道"要去落库等审批"，
        # 而不是把 waiting_approval 当成又一个失败状态。
        if self.pending_approval is not None:
            result["approval"] = self.pending_approval
        return StopEvent(result=result)
