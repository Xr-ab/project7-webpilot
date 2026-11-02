"""任务编排：把 Agent 循环挂到后台跑，边跑边落盘，随时可查。

为什么不直接在 API 里 `await workflow.run()`：
一个任务 5~30 秒，同步等它跑完，浏览器那边的 HTTP 请求就会一直挂着，
前端也得跟着卡——而且用户根本看不到"正在第几步"。
所以：提交接口立刻返回 task_id，真正执行放到别处。

执行放在哪里，有两个模式（P3-M3 之后）：
1. **队列模式**（默认）：`submit` 把任务塞进 arq（redis），由独立的 worker 进程执行。
   好处是并发有上限、任务不会因为 API 重启而死、可以横向加 worker。
2. **进程内模式**（`QUEUE_ENABLED=false`，或 redis 连不上时自动降级）：
   `asyncio.create_task` 直接跑，就是 P1 的行为。
   留着它有两个用处：排查"到底是不是队列的问题"、以及没有 redis 的机器也能跑。

⚠️ 队列模式下有个关键事实：**执行发生在另一个进程**。
所以 API 进程的 `_tasks` 内存字典里不会有正在跑的任务，
进度只能靠 DB（这也是为什么 on_step 里每步都要 bump_steps）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from llama_index.core.workflow import WorkflowTimeoutError

from app.agent.llm import LLMClient
from app.agent.workflow import BrowserAgentWorkflow
from app.browser.session import BrowserSession
from app.config import settings
from app.db import repo
from app.runner import events
from app.security import vault
from app.storage import files
from app.status import APPROVAL_APPROVED, APPROVAL_REJECTED, NEEDS_WORKER, PAUSED

log = logging.getLogger(__name__)

# LLM 客户端单例：内部有连接池，每个任务新建一个纯属浪费
_llm: LLMClient | None = None

# task_id -> meta（实时状态）。这是"活"的数据，磁盘上的 meta.json 是给历史查询用的
_tasks: dict[str, dict] = {}

# arq 的连接池。懒创建：没有 redis 的机器上，这个变量永远是 None，不会报错。
_redis = None
# redis 熔断：连不上之后 60 秒内不再尝试，避免每次提交都白等一次超时
_redis_down_until = 0.0


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


async def get_task(task_id: str) -> dict | None:
    """三级查找：内存（最实时的步数）→ 数据库（跨进程、可查）→ 磁盘（历史归档）。

    为什么内存放第一：进程内模式下，正在跑的任务在内存里是**同一个 dict 对象**，
    execute_task 直接 update 它，永远不会落后于正在发生的状态。

    ⚠️ 但队列模式下（M3 埋过、M5a 验证时踩响的坑）：API 进程的 _tasks 里
    只有 submit 时塞进去的那份 "queued 快照"——真正的执行在 worker 进程，
    它的更新只会落到 DB 和磁盘，API 内存里的 queued 永远不会自己变。
    如果无脑信内存，前端轮询会永远看到 queued，eval 脚本会白等 300 秒超时。

    判别方法：终态（success / step_limit / timeout / loop / …）只可能由
    本进程的 execute_task 写进内存；队列模式下本进程不执行任务，
    内存永远停在 queued/running。所以——**内存里的"未终态"不可信，去 DB 对一次**。
    """
    meta = _tasks.get(task_id)
    if meta is not None and meta.get("status") not in NEEDS_WORKER:
        return meta  # 内存已是终态（含待审批）：只可能来自本进程执行，直接信

    from_db = await repo.fetch_task(task_id)
    if from_db is not None:
        if from_db.get("status") in PAUSED:
            # 待审批信息从哪来，按可靠性排序读：
            # 1. approvals 表（P5a-2 之后的主路径，带 id / 审批人 / 等待时长）
            # 2. meta.json（P5a-1 时代的产物、或表写入失败时的兜底）
            # 为什么不让前端自己调 /approval：任务详情接口是所有页面都要的一个请求，
            # 把审批塞进同一份响应，前端只轮询一个接口就能画完整页面。
            pending = await repo.fetch_pending_approval(task_id)
            if pending is None:
                disk = files.load_meta(task_id) or {}
                pending = disk.get("approval")
            if pending:
                from_db["approval"] = pending
        # P5b：登录态的使用情况只在磁盘 meta 里（tasks 表没有这一列，也不打算加——
        # 它是"这次执行的现场信息"，不是任务属性，加列等于让历史行存一份过期快照）。
        # 和 approval 同一个理由补进响应：前端轮询一个接口就能画完整页面，
        # 也让用户能看懂"为什么它看到的是未登录页面"。
        if "credential" not in from_db:
            disk_credential = (files.load_meta(task_id) or {}).get("credential")
            if disk_credential:
                from_db["credential"] = disk_credential
        # 终态回写缓存：DB 已经完结的任务，下次 GET 不用再打一次 DB
        if from_db.get("status") not in NEEDS_WORKER:
            _tasks[task_id] = from_db
        return from_db

    if meta is not None:
        # DB 不可用（降级模式）时的兜底：进程内降级时内存就是真相，
        # 宁可返回它也不报 500
        return meta
    return files.load_meta(task_id)


async def get_trace(task_id: str) -> list[dict]:
    """同上：DB 优先，读不到再退回 JSON。

    回退不是"以防万一"，而是真实需要：P3 之前跑的任务只有 JSON，
    迁移时并不导入历史数据（见计划里的"不做的事"），所以老任务必须能读。
    """
    rows = await repo.fetch_steps(task_id)
    if rows:
        return rows
    return files.load_trace(task_id)


async def _enqueue(task_id: str, goal: str, start_url: str) -> bool:
    """把任务塞进 arq 队列。返回是否成功；失败由调用方降级成进程内执行。

    为什么失败要降级而不是报错：队列是"更好"，不是"必须"。
    redis 没起的时候，用户提个任务不该被拒绝 —— 那会把一个优化变成新的单点故障。
    """
    global _redis, _redis_down_until

    if not settings.queue_enabled:
        return False
    if time.monotonic() < _redis_down_until:
        return False

    try:
        if _redis is None:
            from arq.connections import RedisSettings, create_pool

            _redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        # 函数名要和 worker 里注册的名字一致（app/worker.py 的 WorkerSettings.functions）
        await _redis.enqueue_job("run_task", task_id, goal, start_url)
        return True
    except Exception as exc:
        # 熔断 60 秒：redis 挂着的时候不能让每次提交都白等一次连接超时
        _redis_down_until = time.monotonic() + 60
        log.warning("入队失败，降级为进程内执行：%s", exc)
        return False


async def _enqueue_resume(task_id: str, approval_id: int) -> bool:
    """把"人处理完审批后的续跑"塞进队列。和 _enqueue 同一个降级策略。

    为什么把 approval_id 一起传进 job：worker 靠它去库里取**人决定的那一条**。
    不传的话 worker 只能取"任务最新的 pending"，那会在
    "批完第 4 步、续跑又触发第 7 步"时取错（见 repo.fetch_approval 的注释）。

    job_id 显式指定（`resume:{task_id}:{approval_id}`）是**去重**：
    arq 里同 job_id 的 job 只会存在一个。两个人几乎同时点批准（或前端重试）
    也只会跑一次续跑——否则同一个动作会被执行两遍，对"提交订单"是灾难。
    """
    global _redis, _redis_down_until

    if not settings.queue_enabled:
        return False
    if time.monotonic() < _redis_down_until:
        return False

    try:
        if _redis is None:
            from arq.connections import RedisSettings, create_pool

            _redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await _redis.enqueue_job(
            "resume_task", task_id, approval_id, _job_id=f"resume:{task_id}:{approval_id}"
        )
        return True
    except Exception as exc:
        _redis_down_until = time.monotonic() + 60
        log.warning("续跑入队失败，降级为进程内执行：%s", exc)
        return False


async def submit(
    *,
    goal: str,
    start_url: str,
    user_id: int | None = None,
    require_approval: bool = True,
    credential_id: int | None = None,
    vision: bool = False,
) -> str:
    """建任务记录并立刻返回 task_id，执行交给队列（或降级为进程内）。

    user_id：登录用户的 id。None = 认证关闭，挂占位用户。
    meta 里带 user_id 存进内存和 JSON：DB 不可用（降级）时，
    API 进程靠内存/文件里的这个字段做归属判断——隔离不能依赖 DB 可用。

    require_approval：这个任务要不要走审批闸门（P5a）。
    为什么做成**任务级**而不是只靠进程级开关：执行发生在 worker 进程，
    批处理脚本（eval_real / stress / collect_corpus）是客户端，
    它在自己进程里改环境变量对 worker 毫无影响。
    而"这个任务需不需要人批"本来就是任务的属性——"帮我下单"要批，
    "列出所有产品名称"不用。所以让它跟着任务走。
    """
    task_id = files.new_task_id()
    meta = {
        "task_id": task_id,
        "user_id": user_id,
        "goal": goal,
        "start_url": start_url,
        "status": "queued",
        "model": settings.llm_model,
        "steps": 0,
        "answer": "",
        "note": "",
        # 跟着 meta 走，worker 从磁盘读回来就知道该不该拦（队列模式下 API 与 worker 不同进程）
        "require_approval": require_approval,
        # P5b：用哪份登录态开局。同样跟着 meta 走，而不是塞进 job 参数——
        # 加一个 job 参数就得同时改 _enqueue、worker.run_task、execute_task 三处签名，
        # 而 meta 本来就要跨进程读一次（队列模式下 execute_task 必须从磁盘恢复现场）。
        # 少一条参数链，就少一处"改了 A 忘了 B"的地方。
        "credential_id": credential_id,
        # P6a：任务级视觉开关。同样跟着 meta 走（理由同上）。
        # 默认 false——视觉会改 prompt，批量评测默认不该带上它。
        "vision": vision,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "elapsed_s": None,
    }
    _tasks[task_id] = meta
    files.save_meta(task_id, meta)
    # 先落 DB 再入队：反过来的话，worker 可能比 API 还快，
    # 它去更新一个还不存在的任务行（虽然 update 会静默 0 行，但状态就永远停在 queued）。
    await repo.create_task(task_id=task_id, goal=goal, start_url=start_url,
                           model=settings.llm_model, user_id=user_id)

    if not await _enqueue(task_id, goal, start_url):
        # 降级路径：和 M3 之前完全一样，进程内跑
        asyncio.create_task(execute_task(task_id, goal, start_url))
    return task_id


async def resume(task_id: str, approval_id: int) -> None:
    """人处理完审批之后：把任务推回 queued，并重新入队一个续跑 job（P5a-3 / P5a-4）。

    批准和拒绝都走这里——"人已经做了决定，任务该往前走了"这件事对两者是一样的，
    区别（执行 or 不执行）由 execute_resume 按审批项状态去分。

    调用方（API）已经先把 approvals.status 改成终态了。这里的顺序是刻意的：

    1. **先改状态，再入队**。反过来的话 worker 可能抢先跑起来把状态写成 running，
       我们再补一个 queued 上去覆盖它 → 任务实际在跑，界面永远停在"排队中"。
    2. **入队失败要退回去**，异常抛给 API，由它把 approvals.status 也退回 pending。
       两边一起退，用户才能再点一次。只退一边的后果是"界面什么都没错，就是不动"。
    """
    meta = _tasks.get(task_id) or files.load_meta(task_id) or {}
    meta["status"] = "queued"
    # 待审批快照用完即弃：它描述的是"暂停那一刻"，续跑之后就是错的信息。
    # 不清掉的话前端会看到一个 queued 的任务还带着 approval 字段，
    # 误以为还在等人批（这就是 P5a-2 踩过的"幻影待审批"）。
    meta.pop("approval", None)
    files.save_meta(task_id, meta)
    await repo.set_status(task_id, "queued")

    if await _enqueue_resume(task_id, approval_id):
        return
    # 降级：没有 redis 的机器上也必须能续跑（和 submit 的策略一致）
    asyncio.create_task(execute_resume(task_id, approval_id))


async def execute_resume(task_id: str, approval_id: int, *, browser=None) -> None:
    """续跑执行入口：按 approval_id 取出人做出的决定，交给 execute_task 接着跑。

    为什么单独一个函数而不是让 execute_task 自己查库：
    execute_task 的职责是"跑一次运行"，它不该知道 approvals 表的存在。
    这里做一次转换（库行 → resume 状态），execute_task 只认那份纯数据，
    单测时塞一个字典就能测，不用建库。

    P5a-4 起这里要处理**两种**决定，而不是只有批准：
    approved → 执行那个动作；rejected → 不执行，把理由回喂模型重新决策。
    两条路的前置完全一样（取审批项、校验状态、组装 resume_state、调 execute_task），
    所以不拆成两个函数——拆了就会有两份一模一样的取数逻辑，改一处忘一处。
    """
    approval = await repo.fetch_approval(approval_id)
    if approval is None:
        # 拿不到审批项 → 不可能知道要执行什么。**绝不能改成"从头重跑"**：
        # 那等于绕开审批，把没人批准的动作做了——失败远比这个后果轻。
        await _fail_resume(task_id, "续跑失败：找不到审批记录")
        return

    if approval["status"] not in (APPROVAL_APPROVED, APPROVAL_REJECTED):
        # 只认两种终态。pending 说明有人在入队之后又把记录退回去了（入队失败重试的竞态），
        # cancelled 说明这条已经作废——两者都**不能执行任何东西**，
        # 也不该把任务丢在 queued 上：按还有没有别的待审批项决定去向。
        pending = await repo.fetch_pending_approval(task_id)
        if pending is not None:
            await repo.set_status(task_id, "waiting_approval")
            _restore_meta_status(task_id, "waiting_approval")
        else:
            await _fail_resume(
                task_id, f"续跑失败：审批项状态是 {approval['status']}，不是终态决定"
            )
        return

    meta = files.load_meta(task_id) or {}
    resume_state = {
        # 人的决定。用布尔而不是再立一套 approve/reject 词表：
        # 审批项状态已经是唯一真相，多一套词就多一处会漂移的映射。
        "rejected": approval["status"] == APPROVAL_REJECTED,
        # 要执行的动作：批准时它就是人批的那一个，不重新问模型；
        # 拒绝时它只用来告诉模型"你上次想干什么被否了"，不执行。
        "action": approval["action"],
        # note 在两种决定下是两种东西——拒绝时它是**会进 prompt 的语义输入**
        # （模型靠它知道为什么被拒），批准时只是审计备注。同一个库列，由 rejected 区分。
        "note": approval["note"] or "",
        # 回到哪个页面：浏览器是新开的，只能靠地址回现场
        "snapshot_url": approval["snapshot_url"],
        # goal / plan / progress / step_no / form_state 都在这份上下文里
        "context": approval["context"],
        # 已经写下的轨迹：不接上它，续跑的 trace 会从第 1 步重来、前 N 步证据全丢
        "trace": files.load_trace(task_id),
    }
    await execute_task(
        task_id,
        str(meta.get("goal") or ""),
        str(meta.get("start_url") or ""),
        browser=browser,
        resume=resume_state,
    )


def _restore_meta_status(task_id: str, status: str) -> None:
    """把状态写回 meta.json（DB 那边由调用方负责）。

    为什么两个都要写：DB 是跨进程的真相，meta.json 是"DB 挂了也能读"的兜底。
    只写一边的话，降级模式（没装数据库的机器）下前端会看到错误的状态。
    """
    meta = files.load_meta(task_id)
    if meta is None:
        return
    meta["status"] = status
    files.save_meta(task_id, meta)


def _exc_text(exc: BaseException, limit: int = 300) -> str:
    """把异常压成一句能写进 note 的说明。

    为什么要截断：异常信息可以很长（SQL 报错能带上整条语句），也可能夹带请求参数。
    note 是要**给用户看**的，不是给开发者看的——完整堆栈留给 log.exception，
    那边只有我们看得到。
    """
    text = f"{type(exc).__name__}: {exc}"
    return text[:limit] + ("…" if len(text) > limit else "")


async def _mark_error(task_id: str, note: str) -> None:
    """把任务钉成 error 终态：meta.json 和 DB 都写。

    为什么抽成一个函数：现在有两个地方要用它——"续跑没法继续"（`_fail_resume`）
    和"job 层兜底"（`mark_crashed`）。两处各写一遍的话，
    `finished_at` 或某个状态字段一定会有一处漏掉，而漏掉的那一处表现为
    "任务没有结束时间"——统计里看不出它是谁写的。
    """
    meta = files.load_meta(task_id) or {}
    meta.update(
        {
            "status": "error",
            "note": note,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    files.save_meta(task_id, meta)
    await repo.set_status(task_id, "error", note=note)


async def _fail_resume(task_id: str, note: str) -> None:
    """续跑没法继续时，把任务标成诚实的失败。

    为什么不用 error 之外的状态：这就是 error —— 有东西坏了。
    用 waiting_approval 会让人以为"还能再批一次"，实际批了也没用。
    """
    await _mark_error(task_id, note)
    log.warning("任务 %s 续跑失败：%s", task_id, note)


async def mark_crashed(task_id: str, exc: BaseException) -> None:
    """job 层兜底：连 `execute_task` 都没接住的异常，也要留下一个看得见的终态。

    为什么需要它（P5b 实测踩到的，见踩坑日志）：
    `execute_task` 里所有执行期代码现在都在 `try` 内，正常不该有异常逃出去。
    但"正常"是靠人守的——P5b 我就把一段准备代码放到了 `try` 外面，
    结果 `AttributeError` 一路冲出 `execute_task`、冲出 job 函数，被 arq 记成 job failed。
    而 **arq 根本不知道我们数据库里有个任务**：它不会去把 status 从 running 改成 error。

    于是任务永久停在 running——前端一直转圈、meta 里没有 note、事后零线索。
    这比"明确报错"糟得多：报错至少告诉你此路不通；"看起来还在跑"是无限等待。

    有了这道兜底，**无论谁在哪一层写漏了 try**，任务最终都会有一个看得见的 error 终态。
    代价只是 note 那句话不如专门写的准确（它只知道"炸了"，不知道"为什么炸"）。
    """
    note = f"执行进程内部错误（{_exc_text(exc)}）"
    try:
        await _mark_error(task_id, note)
    except Exception:
        # 兜底自己也失败（DB 和磁盘同时不可用）：只能记日志。
        # 这里**绝不能**再抛——那会掩盖真正的原始异常，让排查从"一个 bug"变成"两个"。
        log.exception("兜底写终态也失败了：task=%s", task_id)
    log.error("任务 %s 的 job 崩溃，已兜底写成 error：%s", task_id, note)


async def _resolve_storage_state(
    meta: dict, *, user_id: int | None
) -> tuple[dict | None, dict]:
    """把 meta 里的 credential_id 变成可以直接塞进 new_context 的 storage_state。

    返回 `(storage_state, report)`：
    - storage_state 为 None 表示"这次匿名跑"，任务照常执行
    - report 是写进 meta["credential"] 的状态说明，给前端显示用

    🔴 这里最重要的决策：**凭证出问题不阻断任务**。

    想反了会怎样：解密失败就 raise，用户提交了一个"读订单"的任务，
    结果因为几个月前存的登录态过期而整个报错。他真正想做的事（读公开页面）
    本来是能成的，是我们把它弄挂了。
    而"静默不报"也不行——那会让他看到"未登录"的结果却不知道为什么。
    所以第三条路：**任务照跑，但把状态明确记下来，前端能看见**。

    为什么不复用 repo.fetch_credential 的 user_id 过滤：
    这里再过滤一次是纵深防御（API 提交时已经查过归属了）。
    多一道的成本是一次 WHERE，而少一道的后果是"猜个 id 就能用别人的账号跑任务"。
    """
    credential_id = meta.get("credential_id")
    if not credential_id:
        return None, {}

    row = await repo.fetch_credential(int(credential_id), user_id)
    if row is None:
        # 保存之后被删了（用户删了，或 CASCADE 带走的）——这不是错误，是正常时序
        return None, {
            "credential_id": credential_id,
            "ok": False,
            "note": "这份登录态已经不在了（可能被删除），本次按未登录执行",
        }

    try:
        state = vault.decrypt(row["ciphertext"])
    except Exception as exc:
        # VaultError（密钥轮换 / 数据损坏）都到这里。**只记类型，不记密文**：
        # 密文进日志等于把凭证的完整副本搬到日志文件里，那是最常见的凭证泄露方式。
        log.warning(
            "登录态解密失败：credential=%s err=%s", credential_id, type(exc).__name__
        )
        return None, {
            "credential_id": credential_id,
            "ok": False,
            "label": row["label"],
            "note": "登录态无法解密（密钥已更换或数据损坏），本次按未登录执行；请重新保存",
        }

    # 过期不拦：cookie 的 expires 是个"声明"，很多站点实际容忍更久；
    # 反过来也有"没过期但服务端早就把它踢了"的情况。所以它只是个提示，不是判据。
    #
    # ⚠️ 用 row["expired"] 而不是自己拿 expires_at 和时间比。第一版写的是
    # `row["expires_at"].timestamp()`，直接 AttributeError —— 因为 _credential_dict
    # 把时间格式化成了 ISO 字符串（列表接口要的是字符串）。
    # 教训不是"记得它是字符串"，而是：**这个判定已经有唯一实现了**
    # （repo._credential_dict 里算好的 expired），旁边再写一份必然两边漂移。
    note = ""
    if row.get("expired"):
        note = "这份登录态可能已过期，如果看到登录页请重新保存一次"

    await repo.touch_credential(int(credential_id))
    return state, {
        "credential_id": credential_id,
        "ok": True,
        "label": row["label"],
        "site": row["site"],
        "note": note,
    }


async def execute_task(
    task_id: str, goal: str, start_url: str, *, browser=None, resume: dict | None = None
) -> None:
    """真正干活的地方。所有异常都必须在这里收干净，否则任务会静默失败。

    参数 browser：非 None 时借用这个浏览器（arq worker 里共享同一个 Chromium 进程），
    None 就自己起一个（进程内模式、脚本调试）。
    参数 resume：非 None 表示这是"人批准之后接着跑"（P5a-3），里面带
    待执行动作 + 暂停页地址 + 决策上下文 + 已有轨迹。

    ⚠️ 不能假设 `_tasks[task_id]` 存在：队列模式下这个进程根本没跑过 submit。
    所以 meta 要能从磁盘读回来，这是"执行可以发生在另一个进程"的前提。
    """
    meta = _tasks.get(task_id)
    if meta is None:
        meta = files.load_meta(task_id) or {
            "task_id": task_id,
            "goal": goal,
            "start_url": start_url,
            "status": "queued",
            "model": settings.llm_model,
            "steps": 0,
            "answer": "",
            "note": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "elapsed_s": None,
        }
        _tasks[task_id] = meta

    meta["status"] = "running"
    files.save_meta(task_id, meta)
    await repo.mark_running(task_id)

    started = time.perf_counter()
    task_root = files.task_dir(task_id)

    # 续跑之前已经跑过一段：这一段耗时不能丢，否则"等审批 1 小时"的任务
    # 会显示成"耗时 3 秒"——统计里最难解释的一类假数据。
    elapsed_before = float(meta.get("partial_elapsed_s") or 0.0) if resume else 0.0

    # 已经写进 DB 的行数。on_step 每次拿到的是**完整** trace 列表，
    # 只插新增的那几行 —— 否则第 n 步要把前 n 条重写一遍（O(n²)）。
    #
    # ⚠️ 续跑时起点不是 0 而是"已存在的行数"：resume 把磁盘上的 trace 接了进来，
    # 如果这里还从 0 开始，第一次 flush 就会把前 N 行**再插一遍**。
    # 表上没有唯一约束（同一步允许多次 attempt），所以不会报错——只会静默翻倍，
    # 然后"数据库比 trace.json 多一倍的行"，这种不一致极难查。
    saved = len(resume.get("trace") or []) if resume else 0

    async def on_step(trace: list[dict]) -> None:
        nonlocal saved
        files.save_trace(task_id, trace)
        head = saved
        saved = len(trace)
        if saved > head:
            await repo.insert_steps(task_id, trace[head:saved])
            # P6b-B3：把新增的步骤推给语音会话（没人在听时 publish_event 自己跳过，
            # 所以这里不需要判断"有没有订阅者"——那一层放在 events.py 里只写一次）
            for row in trace[head:saved]:
                await events.publish_event(task_id, events.step_event(row))
        meta["steps"] = len(trace)
        files.save_meta(task_id, meta)
        # 队列模式下 API 进程看不到内存里的 steps，必须写进 DB 前端才看得到进度
        await repo.bump_steps(task_id, len(trace))

    async def on_approval(pending: dict) -> None:
        """闸门停下时调用：把待审批信息落库 + 落 meta（P5a）。

        两个落点，职责不同（不是重复）：
        - `approvals` 表：**审批记录**。一人一行、可查可审计，P5a-3 的批准接口
          靠它的 id 定位"批的是哪一条动作"，拒绝理由也存在这里。
        - meta.json：**任务当前状态**的快照。前端 GET /tasks/{id} 一次就能拿到
          "在等什么"，不必再多打一个审批接口；DB 不可用时它是唯一来源。

        顺序：先表后文件。表写失败时 meta 仍然会写成功，
        所以最坏情况是"审计缺一条、功能不受影响"，而不是"功能没了"。
        """
        # 表写失败只记日志、返回 None，不影响暂停本身——
        # 高风险动作不能因为数据库抖动就放行（那才是最坏的结果）
        approval_id = await repo.create_approval(
            task_id=task_id,
            step_no=int(pending.get("step_no") or 0),
            action=pending.get("action") or {},
            reason=pending.get("reason") or "",
            snapshot_url=pending.get("snapshot_url") or "",
            context=pending.get("context") or {},
        )
        # 把 id 带回 meta：P5a-3 批准时要按 id 校验"批的是当前这一条"，
        # 没有 id 就只能按 step_no 猜，同一步被拒两次时会批错记录
        pending = {**pending, "id": approval_id}
        meta["approval"] = pending
        files.save_meta(task_id, meta)
        # 状态先落一份：前端轮询到 waiting_approval 就会停下来显示审批卡片，
        # 不必等整个 workflow 收尾（收尾还要关浏览器，有几百毫秒延迟）
        meta["status"] = "waiting_approval"
        await repo.set_status(task_id, "waiting_approval")

    # P5b：这一趟以什么身份开局。None = 匿名，也就是 P5b 之前的老行为。
    #
    # 为什么要读 meta 而不是走 job 参数：续跑（execute_resume → execute_task）走的是
    # 同一条路径，而续跑时 job 只带 approval_id，拿不到原来的提交参数。
    # 只有 meta 是"每次执行都能读到"的那份真相。
    #
    # ⚠️ 这一段**必须在 try 里面**，第一版我把它放在 try 外面（理由写的是"这不是
    # 执行中出的错"）。结果它里有一处 AttributeError，异常直接冒出 execute_task：
    # 任务永远停在 running、meta 里没有任何 note、前端一直转圈等一个不会来的终态。
    # 这就是最坏的一种失败——**没有留下任何可查的痕迹**。
    # 结论：凡是"执行期"的代码，不管它是准备工作还是正活，都得在能兜底的 try 里。
    try:
        storage_state, credential_report = await _resolve_storage_state(
            meta, user_id=meta.get("user_id")
        )
        if credential_report:
            # 单独一个字段，不占用 meta["note"]：note 是任务结论的一部分，
            # 会被结果覆盖；这一条要一直留着，前端才能解释"为什么它看到的是未登录页面"
            meta["credential"] = credential_report
            files.save_meta(task_id, meta)

        async with BrowserSession(browser=browser, storage_state=storage_state) as session:
            workflow = BrowserAgentWorkflow(
                session=session,
                llm=get_llm(),
                shot_dir=task_root,   # 每步截图直接落进任务目录
                on_step=on_step,
                # P5a：闸门停下时回调，把"待审批动作 + 恢复上下文"落盘
                on_approval=on_approval,
                # 任务级开关从 meta 读（队列模式下 API 写 JSON、worker 读 JSON）
                approval_enabled=bool(meta.get("require_approval", True)),
                # P6a：任务级视觉强制（canvas 演示/评测对比）。总闸仍在 settings 里，
                # workflow 内部做"与"判断——总闸关着时这里 true 也不看。
                vision=bool(meta.get("vision", False)),
                # P5a-3：带着续跑状态启动。非 None 时 step_start 走恢复分支
                # （回到暂停页 → 执行被批准的动作 → 接着循环）
                resume=resume,
            )
            result = await workflow.run(goal=goal, start_url=start_url)
    except WorkflowTimeoutError:
        result = {
            "status": "timeout",
            "answer": "",
            "steps": len(files.load_trace(task_id)),
            "note": f"超过 {settings.task_timeout_seconds}s 仍未结束",
            "trace": files.load_trace(task_id),
        }
    except Exception as exc:  # 兜底：任何没预料到的崩溃都要留痕，不能静默
        result = {
            "status": "error",
            "answer": "",
            "steps": len(files.load_trace(task_id)),
            "note": f"{type(exc).__name__}: {exc}",
            "trace": files.load_trace(task_id),
        }

    # 这一趟是不是"暂停等审批"？两种情况收尾方式不同：
    # 暂停不能写 finished_at / elapsed_s（任务还没结束，批准后还要接着跑），
    # 也不能走 finish_task——那会把两个时间字段钉死，续跑后统计会看到
    # "耗时 3 秒、完成时间在 10 分钟前"的怪任务。
    paused = result["status"] in PAUSED
    # 这一趟的实际耗时。续跑时要加上暂停前那一段，否则"等审批 1 小时的任务"
    # 会被记成"耗时 3 秒"——统计里最难解释的一类假数据。
    segment = round(time.perf_counter() - started, 2)
    total_elapsed = round(elapsed_before + segment, 2)

    meta.update(
        {
            "status": result["status"],
            "steps": result["steps"],
            "answer": result["answer"],
            "note": result.get("note", ""),
            # 把模型自己定的任务分解也存下来。
            # 为什么要存：加了 plan/progress 机制之后，"效果好不好"取决于**模型有没有真的给 plan**。
            # 只存 status 的话，一旦没涨分，就分不清是"机制没用"还是"模型没遵守"——
            # 这两种结论指向完全相反的下一步。
            "plan": result.get("plan", []),
            "progress": result.get("progress", []),
            "finished_at": None if paused else time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": None if paused else total_elapsed,
            # 暂停：把"已经跑了多久"另存一份。
            # 为什么不直接用 elapsed_s：elapsed_s 的语义是"任务总耗时"，暂停时写它
            # 等于宣布任务结束，统计会把待审批任务算进已完成。分两个字段，
            # 一个给当前这一趟用，一个给最终汇总用。
            **({"partial_elapsed_s": total_elapsed} if paused else {}),
        }
    )
    if paused:
        # 待审批信息要落在 meta 里：前端不查审批表也能立刻渲染"在等什么动作"
        meta["approval"] = result.get("approval")
    files.save_meta(task_id, meta)
    files.save_trace(task_id, result.get("trace", []))

    # 兜底补写：把还没落库的 trace 行补进去。
    # 为什么需要这一层（workflow 里已经修了 finish 不 flush 的问题）：
    # 还有别的退出路径不走 on_step —— 超时、没预料到的异常，
    # 它们留下的行只存在于 `result["trace"]`。少了这步，DB 会比 trace.json 少几行，
    # 而"数据库和文件对不上"是最难查的一类问题：不知道该信哪个。
    tail = result.get("trace") or []
    if len(tail) > saved:
        await repo.insert_steps(task_id, tail[saved:])

    if paused:
        # 暂停：只改状态，不写 finished_at（任务还没结束）。
        # on_approval 里已经写过一次状态——这里是兜底，防止 on_approval 回调
        # 因为 DB 抖动没写成功时，任务状态停在 running 上永远不动。
        await repo.set_status(task_id, meta["status"], note=meta["note"])
        # P6b-B3：待审批也是"这一趟结束"，语音会话要收到 final 才能
        # 显示"去网页上批准"而不是干等（见 events._FINAL_STATUSES 的说明）
        await events.publish_event(task_id, events.final_event(meta))
        return

    # 终态写 DB。放在 JSON 之后：JSON 是"一定能写成"的那一份，
    # DB 是增强项，顺序反过来的话 DB 失败会让人误以为任务本身出了问题。
    await repo.finish_task(
        task_id,
        status=meta["status"],
        answer=meta["answer"],
        note=meta["note"],
        steps=meta["steps"],
        plan=meta.get("plan") or [],
        progress=meta.get("progress") or [],
        elapsed_s=meta["elapsed_s"],
    )

    # P6b-B3：终态事件。放在最后——DB/文件都写完了，前端拿到 final 立刻
    # 去查详情时不会读到半截状态（事件比数据先到是最烦的一类竞态）。
    await events.publish_event(task_id, events.final_event(meta))
