"""数据库读写：把 runner / API 要的操作收敛到这一层。

它在项目里的角色：
任务的生命周期（提交 → 跑 → 结束 → 查询）原来全在 `task_runner` 里操作内存和 JSON。
M2 之后这些动作有两条落点（PG + JSON 双写），但**业务代码不应该知道这件事**，
所以所有 SQL 都收在 `repo` 里，上层只调用 `create_task` / `finish_task` 这类语义函数。

两个刻意的设计：

1. **DB 故障绝不拖垮任务**。每个函数失败都只记日志、返回 None/0，由调用方退回 JSON。
   理由：P1/P2 的功能本来就不依赖数据库，M2 引入的是一层额外保障，不是新的硬依赖。
   如果数据库挂了任务也跑不了，那是把可用性往下拉了。

2. **失败后短暂"熔断"**。数据库没起时，每次请求都去连一遍会白等几秒
   （asyncpg 建连接有超时）。所以在进程内记一个 `_db_down_until`，
   60 秒内直接跳过 DB，走 JSON。这样"没装数据库"的机器体验和 M1 之前完全一样。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import Integer, delete, func, select, text, update

from app.db.models import Approval, Credential, Task, TaskStep, User
from app.db.session import SessionLocal
from app.status import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    NEEDS_WORKER,
    UNFINISHED,
)

log = logging.getLogger(__name__)

# P3 双写期先挂在这个占位用户名下（tasks.user_id 是 NOT NULL 外键）。
# M4 接 JWT 之后，这里的调用会被替换成真实登录用户；到那时这个函数只服务
# "命令行/脚本直接提交任务"的场景。
LOCAL_USER_EMAIL = "local@webpilot.dev"

# 熔断到期时间（monotonic 秒）。0 表示可用。
_db_down_until = 0.0
_FUSE_SECONDS = 60.0


def _fuse_off() -> None:
    global _db_down_until
    _db_down_until = time.monotonic() + _FUSE_SECONDS


def db_usable() -> bool:
    """数据库此刻是否值得尝试。False 时调用方应直接走 JSON 回退。"""
    return time.monotonic() >= _db_down_until


def _fmt(dt: datetime | None) -> str | None:
    """统一成和 meta.json 里一样的**本地时间**字符串，前端不用分辨数据来自哪。

    为什么必须转时区：TIMESTAMPTZ 从 asyncpg 取回来是**带 UTC 时区**的 datetime，
    直接 strftime 得到的是 UTC 时间。而 P1 的 JSON 用的是 `time.strftime()`（本地时间）。
    不转的话，同一个任务从 DB 读显示 05:42、从文件读显示 13:42 —— 差 8 小时，
    而且是那种"看起来像数据错乱"的差，排查起来很浪费时间。
    """
    if dt is None:
        return None
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


async def ensure_local_user() -> int:
    """拿到（必要时创建）占位用户的 id。幂等，可以每次调用。"""
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL, password_hash="!")  # ! 表示不可登录
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user.id


async def create_task(
    *,
    task_id: str,
    goal: str,
    start_url: str,
    model: str,
    user_id: int | None = None,
) -> bool:
    """任务提交时先落一行 queued。返回是否成功（失败不影响任务继续跑）。

    user_id：登录用户的 id。传 None 时挂到占位用户 local@webpilot.dev 名下——
    这是 AUTH_ENABLED=false（或脚本直调 repo）时的行为，M4 之前所有任务都走这条路。
    """
    if not db_usable():
        return False
    try:
        if user_id is None:
            user_id = await ensure_local_user()
        async with SessionLocal() as session:
            session.add(
                Task(
                    id=task_id,
                    user_id=user_id,
                    goal=goal,
                    start_url=start_url,
                    status="queued",
                    model=model,
                )
            )
            await session.commit()
        return True
    except Exception as exc:
        _fuse_off()
        log.warning("写 tasks 失败（任务继续跑，退回 JSON）：%s", exc)
        return False


async def mark_running(task_id: str) -> None:
    if not db_usable():
        return
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(status="running", started_at=func.now())
            )
            await session.commit()
    except Exception as exc:
        _fuse_off()
        log.warning("更新 running 失败：%s", exc)


async def set_status(task_id: str, status: str, note: str | None = None) -> None:
    """只改状态（不动 finished_at / elapsed_s 这些"收尾"字段）。

    为什么不能复用 finish_task：审批停等既不是完成也不是失败，
    它是"任务还活着，只是在等人"。用 finish_task 会把 finished_at 和耗时写死，
    等用户批准续跑后，这两个字段就再也不会更新了——统计会看到一个
    "耗时 3 秒、已完成时间在 10 分钟前"的怪任务。

    note 传 None 表示不改 note（默认），避免把已有的失败原因覆盖成空。
    """
    if not db_usable():
        return
    try:
        values: dict = {"status": status}
        if note is not None:
            values["note"] = note
        async with SessionLocal() as session:
            await session.execute(
                update(Task).where(Task.id == task_id).values(**values)
            )
            await session.commit()
    except Exception as exc:
        _fuse_off()
        log.warning("更新状态 %s 失败：%s", status, exc)


async def insert_steps(task_id: str, rows: list[dict]) -> int:
    """把新增的 trace 行插进 task_steps。返回插入条数。

    为什么要算 attempt 而不是直接插入：`_record_rejected()` 会让"被拒绝的决策"
    和"重试后的决策"共用同一个 step_no，表上没有唯一约束，
    所以同一步可能出现多条。attempt 是它们的区分依据，也是"被拒几次"的计数。
    这里按"同 task_id + 同 step_no 已有几条"递增，跨批次调用也能算对。
    """
    if not rows or not db_usable():
        return 0
    try:
        async with SessionLocal() as session:
            existing = await session.execute(
                select(TaskStep.step_no, func.max(TaskStep.attempt))
                .where(TaskStep.task_id == task_id)
                .group_by(TaskStep.step_no)
            )
            max_attempt: dict[int, int] = {no: (mx or 0) for no, mx in existing.all()}

            objs = []
            for row in rows:
                step_no = int(row.get("step") or 0)
                attempt = max_attempt.get(step_no, 0) + 1
                max_attempt[step_no] = attempt
                usage = row.get("llm_usage") or {}
                objs.append(
                    TaskStep(
                        task_id=task_id,
                        step_no=step_no,
                        attempt=attempt,
                        url=str(row.get("url") or ""),
                        thought=str(row.get("thought") or ""),
                        action=str(row.get("action") or ""),
                        ok=bool(row.get("ok")),
                        error=row.get("error"),
                        duration_ms=int(row.get("duration_ms") or 0),
                        elements_total=int(row.get("elements_total") or 0),
                        screenshot=row.get("screenshot"),
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        # P6a：视觉留痕（没看图时是 None）。字段形状见 models.TaskStep
                        vision=row.get("vision"),
                    )
                )
            session.add_all(objs)
            await session.commit()
            return len(objs)
    except Exception as exc:
        _fuse_off()
        log.warning("写 task_steps 失败（任务继续跑）：%s", exc)
        return 0


async def finish_task(
    task_id: str,
    *,
    status: str,
    answer: str,
    note: str,
    steps: int,
    plan: list,
    progress: list,
    elapsed_s: float | None,
) -> None:
    """任务结束：写终态 + 汇总 token。

    token 汇总在这里算一次（SUM 一遍 task_steps），存到 tasks 上。
    为什么冗余存：/stats 接口要按任务列表统计成本，每次都 SUM 子表会慢一个量级。
    """
    if not db_usable():
        return
    try:
        async with SessionLocal() as session:
            totals = await session.execute(
                select(
                    func.coalesce(func.sum(TaskStep.prompt_tokens), 0),
                    func.coalesce(func.sum(TaskStep.completion_tokens), 0),
                ).where(TaskStep.task_id == task_id)
            )
            prompt_tokens, completion_tokens = totals.one()
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=status,
                    answer=answer,
                    note=note,
                    steps=steps,
                    plan=plan,
                    progress=progress,
                    elapsed_s=elapsed_s,
                    finished_at=func.now(),
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                )
            )
            await session.commit()
    except Exception as exc:
        _fuse_off()
        log.warning("写任务终态失败：%s", exc)


async def fetch_stats(since: datetime | None = None, user_id: int | None = None) -> dict | None:
    """聚合统计（M5b `/api/v1/stats` 的数据来源）。失败返回 None，调用方降级。

    user_id：登录用户的隔离过滤——看板只能看到自己的任务。
    别人的任务数混进自己的成功率里，是比"看不到"更糟的泄漏。
    None 表示不过滤（本地模式 / 脚本）。

    - 分母 = **终态任务**（status 不在 queued/running）。还没跑完的尝试既不算
      成功也不算失败，摊进分母会让成功率随"查询时机"抖动——评测跑完的瞬间
      和跑完十秒后查，数字必须一样。
    - elapsed_s 的 P50/P95 用 PostgreSQL 的 percentile_cont（聚合天然忽略 NULL，
      没跑完的任务 elapsed_s 是 NULL，自动不参与）。
    - token 用 tasks 表上的冗余汇总列（finish_task 结束时已经 SUM 好），
      不再扫 task_steps 子表——这正是 M3 冗余那一列的回报。
    - success 只代表 status=success，**不代表答案正确**（eval_real 还会校验答案，
      这层 stats 管不了）；两边交叉验证的是"任务数 / 状态分布 / 步数 / 耗时"。

    since：只统计这之后提交的任务（datetime，字符串解析在路由层做——
    DB 层不该兼职处理用户输入格式；绑定时传 datetime 对象，
    asyncpg 才会用 timestamptz 参数而不是字符串拼接）。
    """
    if not db_usable():
        return None
    try:
        async with SessionLocal() as session:
            # 终态条件：还没出结果的任务（排队/运行/待审批）不进任何统计口径。
            # ⚠️ 待审批必须在这里排除。把它算成终态会让成功率分母混进
            # "还没跑完、只是卡着等人"的任务，P5 加状态时差点漏掉（见 app/status.py）。
            terminal = ~Task.status.in_(UNFINISHED)
            where = terminal
            if user_id is not None:
                where = where & (Task.user_id == user_id)
            since_cond = None
            if since is not None:
                since_cond = Task.created_at >= since
                where = terminal & since_cond

            totals = await session.execute(
                select(
                    func.count(Task.id),
                    func.coalesce(func.avg(Task.steps), 0),
                    func.coalesce(func.percentile_cont(0.5).within_group(Task.elapsed_s), 0),
                    func.coalesce(func.percentile_cont(0.95).within_group(Task.elapsed_s), 0),
                    func.coalesce(func.sum(Task.prompt_tokens), 0),
                    func.coalesce(func.sum(Task.completion_tokens), 0),
                ).where(where)
            )
            total, avg_steps, p50, p95, prompt_tok, completion_tok = totals.one()

            # 失败归因分布：一行一个 status。success 之外的就是失败原因的粗分类
            dist_rows = await session.execute(
                select(Task.status, func.count(Task.id)).where(where).group_by(Task.status)
            )
            distribution: dict[str, int] = {s: int(c) for s, c in dist_rows.all()}

            # 分任务聚合（按 goal 分组）：对应 eval_real 报告里的"分任务"表。
            # 为什么按 goal 而不是 start_url：同一站点上跑不同题（首页提取 vs 翻页）
            # 共用一个 URL，按 URL 分组会把不同难度的题搅在一起；
            # 评测里每道题的 goal 是固定文案，按 goal 分组正好一题一行。
            case = func.sum(func.cast(Task.status == "success", Integer))
            goal_rows = await session.execute(
                select(
                    Task.goal,
                    func.count(Task.id),
                    case,
                    func.avg(Task.steps),
                    func.avg(Task.elapsed_s),
                )
                .where(where)
                .group_by(Task.goal)
                .order_by(func.count(Task.id).desc())
                .limit(20)
            )
            by_goal = [
                {
                    "goal": goal,
                    "total": int(total),
                    "success": int(success),
                    "success_rate": round(int(success) / int(total) * 100, 1) if int(total) else 0.0,
                    "avg_steps": round(float(steps), 1) if steps is not None else 0.0,
                    "avg_elapsed_s": round(float(elapsed), 1) if elapsed is not None else 0.0,
                }
                for goal, total, success, steps, elapsed in goal_rows.all()
            ]

            # 未完结数单独数一条：它不进成功率分母，但前端要显示"还有几个没跑完"。
            # 包含待审批（UNFINISHED = 排队/运行/待审批）：从用户视角它就是"还没结果"。
            unfinished_cond = Task.status.in_(UNFINISHED)
            if user_id is not None:
                unfinished_cond = unfinished_cond & (Task.user_id == user_id)
            if since_cond is not None:
                unfinished_cond = unfinished_cond & since_cond
            unfinished = await session.scalar(
                select(func.count(Task.id)).where(unfinished_cond)
            )

            return {
                "total": int(total),
                "unfinished": int(unfinished or 0),
                "success": distribution.get("success", 0),
                "success_rate": round(distribution.get("success", 0) / int(total) * 100, 1)
                if int(total)
                else 0.0,
                "avg_steps": round(float(avg_steps), 2),
                "elapsed_p50_s": round(float(p50), 2),
                "elapsed_p95_s": round(float(p95), 2),
                "tokens": {
                    "prompt": int(prompt_tok),
                    "completion": int(completion_tok),
                    "total": int(prompt_tok) + int(completion_tok),
                },
                "status_distribution": distribution,
                "by_goal": by_goal,
            }
    except Exception as exc:
        _fuse_off()
        log.warning("聚合统计失败：%s", exc)
        return None


async def fetch_task(task_id: str, user_id: int | None = None) -> dict | None:
    """按 meta.json 的形状返回一行任务，供 API 直接吐给前端。

    user_id 传入时做**归属过滤**：不是这个用户的任务一律返回 None，
    上层据此回 404。为什么不返回 403：对调用方来说"任务不存在"和
    "任务存在但不是你的"应该无法区分——403 等于承认任务存在，泄露了它的 id。
    user_id 为 None 表示不过滤（worker / 本地模式 / 管理脚本用）。
    """
    if not db_usable():
        return None
    try:
        async with SessionLocal() as session:
            conditions = [Task.id == task_id]
            if user_id is not None:
                conditions.append(Task.user_id == user_id)
            task = (
                await session.execute(select(Task).where(*conditions))
            ).scalar_one_or_none()
            if task is None:
                return None
            return {
                "task_id": task.id,
                "user_id": task.user_id,
                "goal": task.goal,
                "start_url": task.start_url,
                "status": task.status,
                "model": task.model,
                "steps": task.steps,
                "answer": task.answer,
                "note": task.note,
                "plan": task.plan or [],
                "progress": task.progress or [],
                "created_at": _fmt(task.created_at),
                "finished_at": _fmt(task.finished_at),
                "elapsed_s": task.elapsed_s,
            }
    except Exception as exc:
        _fuse_off()
        log.warning("读任务失败（退回 JSON）：%s", exc)
        return None


async def fetch_task_list(
    user_id: int | None, limit: int = 20, offset: int = 0
) -> dict:
    """按用户列出任务（M4 收尾）。返回 {"items": [...], "total": n}。

    为什么隔离要连"列表"一起做：
    详情接口已经会挡别人的 task_id（404），但列表会把**别人的任务标题和状态**
    直接摊在页面上 —— id 没泄露，内容和存在性却泄露了，隔离就不完整。

    user_id 为 None 表示不过滤：只有"认证关闭"的本地单人模式才会走到这里，
    与 fetch_task / fetch_stats 同一约定。

    为什么返回 total：前端要做"共 N 个任务"和翻页，让前端自己数总数
    会导致它必须把所有页都拉一遍才知道边界。
    """
    if not db_usable():
        return {"items": [], "total": 0}
    try:
        async with SessionLocal() as session:
            conditions = []
            if user_id is not None:
                conditions.append(Task.user_id == user_id)

            total = await session.scalar(
                select(func.count()).select_from(Task).where(*conditions)
            )
            rows = (
                await session.execute(
                    select(Task)
                    .where(*conditions)
                    # id 兜底排序：同一秒创建的两个任务，只按时间排顺序不稳定，
                    # 翻页时会出现同一个任务出现两次、另一个漏掉
                    .order_by(Task.created_at.desc(), Task.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()

            return {
                "items": [
                    {
                        "task_id": t.id,
                        "goal": t.goal,
                        "start_url": t.start_url,
                        "status": t.status,
                        "steps": t.steps,
                        "answer": t.answer,
                        "created_at": _fmt(t.created_at),
                        "elapsed_s": t.elapsed_s,
                    }
                    for t in rows
                ],
                "total": int(total or 0),
            }
    except Exception as exc:
        _fuse_off()
        log.warning("列任务失败（退回空列表）：%s", exc)
        return {"items": [], "total": 0}


async def fetch_steps(task_id: str, user_id: int | None = None) -> list[dict]:
    """按 trace.json 的形状返回每步明细（含 llm_usage 这个嵌套键）。

    user_id 过滤靠 join 回 tasks 表：step 表自己没有 user_id 列
    （每步重复存一份归属信息既浪费又容易漂移），归属只记在 tasks 上。
    """
    if not db_usable():
        return []
    try:
        stmt = (
            select(TaskStep)
            .join(Task, TaskStep.task_id == Task.id)
            .where(TaskStep.task_id == task_id)
            .order_by(TaskStep.step_no, TaskStep.attempt)
        )
        if user_id is not None:
            stmt = stmt.where(Task.user_id == user_id)
        async with SessionLocal() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "step": r.step_no,
                    "attempt": r.attempt,
                    "url": r.url,
                    "thought": r.thought,
                    "action": r.action,
                    "ok": r.ok,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                    "elements_total": r.elements_total,
                    "screenshot": r.screenshot,
                    # 视觉留痕原样带出去（NULL 时保留 None，不塞 {}——
                    # 统计"兜底率"要能区分"没看图"和"看了没看到"）
                    "vision": r.vision,
                    "llm_usage": {
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                    },
                }
                for r in rows
            ]
    except Exception as exc:
        _fuse_off()
        log.warning("读 trace 失败（退回 JSON）：%s", exc)
        return []


async def bump_steps(task_id: str, steps: int) -> None:
    """把当前步数刷到 tasks 上。

    为什么需要（M3 之后才变成问题）：
    进队列之后，跑任务的是**另一个进程**（arq worker），API 进程的内存字典里没有这个任务，
    所以前端轮询只能读 DB。如果步数只在结束时才写，任务跑的过程中前端会一直看到 steps=0，
    看起来像"卡住了"。每步刷一下，代价是一条按主键的 UPDATE。
    """
    if not db_usable():
        return
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Task).where(Task.id == task_id).values(steps=steps)
            )
            await session.commit()
    except Exception as exc:
        _fuse_off()
        log.warning("更新步数失败：%s", exc)


# ---- P5a：审批项（approvals 表） ----


async def create_approval(
    *,
    task_id: str,
    step_no: int,
    action: dict,
    reason: str,
    snapshot_url: str,
    context: dict,
) -> int | None:
    """闸门停下时写一条 pending 审批项，返回它的 id（失败返回 None）。

    为什么失败不让任务崩：审批落库是"增强项"，落不下时 meta.json 里还有一份
    （P5a-1 的行为）。任务该停还是得停——不能因为数据库抖动就放行高风险动作。
    但这意味着调用方必须容忍 None，P5a-3 的批准接口遇到 None 会回退到读 meta。
    """
    if not db_usable():
        return None
    try:
        async with SessionLocal() as session:
            row = Approval(
                task_id=task_id,
                step_no=step_no,
                action=action or {},
                reason=reason or "",
                snapshot_url=snapshot_url or "",
                context=context or {},
                status=APPROVAL_PENDING,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id
    except Exception as exc:
        _fuse_off()
        log.warning("写 approvals 失败（前端仍可从 meta.json 读）：%s", exc)
        return None


async def fetch_pending_approval(
    task_id: str, user_id: int | None = None
) -> dict | None:
    """查这个任务当前待审批的那一条。没有就返回 None（前端渲染成"无待审批"）。

    user_id 过滤靠 join 回 tasks：审批项自己没有归属列（同 task_steps 的处理）——
    一份归属信息只该有一个真相，重复存两份早晚漂移。
    None 表示不过滤（worker / 本地模式）。

    为什么取 created_at 最新的一条而不是报错"有多条"：
    正常情况下同一任务同时只有一条 pending（批准/拒绝都会把上一条置为终态）。
    但历史上出现过脏数据（进程在两次审批之间被 kill），此时**最新的一条才是
    用户该看到的**——直接 500 只会让任务永远卡死，没有可操作性。
    """
    if not db_usable():
        return None
    try:
        stmt = (
            select(Approval)
            .join(Task, Approval.task_id == Task.id)
            .where(Approval.task_id == task_id, Approval.status == APPROVAL_PENDING)
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        if user_id is not None:
            stmt = stmt.where(Task.user_id == user_id)
        async with SessionLocal() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return _approval_dict(row)
    except Exception as exc:
        _fuse_off()
        log.warning("读待审批失败（退回 meta.json）：%s", exc)
        return None


def _approval_dict(row: Approval) -> dict:
    """DB 行 → API 响应。形状和 P5a-1 写进 meta.json 的 pending 保持一致，
    这样前端不用分辨数据来自表还是文件。"""
    waiting_s = None
    if row.created_at is not None:
        # created_at 是 TIMESTAMPTZ（asyncpg 取回来带 UTC 时区），
        # 直接和 datetime.now()（naive 本地时间）相减会抛
        # "can't subtract offset-naive and offset-aware datetimes"。
        # 统一在 UTC 上算，再取整成秒给前端显示"已等待 X 分钟"。
        now = datetime.now(tz=row.created_at.tzinfo)
        waiting_s = max(0, int((now - row.created_at).total_seconds()))
    return {
        "id": row.id,
        "task_id": row.task_id,
        "step_no": row.step_no,
        "action": row.action or {},
        "reason": row.reason,
        "snapshot_url": row.snapshot_url,
        "context": row.context or {},
        "status": row.status,
        "note": row.note,
        "decided_by": row.decided_by,
        "created_at": _fmt(row.created_at),
        "waiting_s": waiting_s,
    }


async def fetch_approval(approval_id: int) -> dict | None:
    """按 id 取一条审批项（worker 续跑时用）。

    为什么要按 id 而不是"取任务最新的那条"：批准动作对应的是**某一条具体记录**。
    按任务取"最新的"会有一个致命竞态——用户在第 4 步批完、任务续跑到第 7 步
    又触发一次审批，如果 worker 取的是"最新 pending"，它可能拿到第 7 步那条、
    却以为那是刚被批准的动作，于是把没人批的动作执行了。
    **审批必须按 id 定位，不能按"当前最新的"定位。**
    """
    if not db_usable():
        return None
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(select(Approval).where(Approval.id == approval_id))
            ).scalar_one_or_none()
            return _approval_dict(row) if row is not None else None
    except Exception as exc:
        _fuse_off()
        log.warning("读审批项失败：%s", exc)
        return None


async def decide_approval(
    approval_id: int,
    *,
    status: str,
    decided_by: int | None = None,
    note: str = "",
) -> bool:
    """把一条 pending 审批改成终态。返回是否**真的改到了**。

    返回值是并发保护的核心：UPDATE 的 WHERE 里带了 `status='pending'`，
    这是一次 compare-and-swap。两个人同时点"批准"时，只有第一个 UPDATE 会命中，
    第二个的 rowcount 是 0 —— 上层据此回 409。

    为什么不用"先 select 再 update"：两步之间有一个窗口，
    两个请求都能读到 pending，然后都去改，任务被续跑两次（第二个会覆盖第一个的
    decided_by，审计链直接错乱）。一条带条件的 UPDATE 就没有这个窗口。
    """
    if not db_usable():
        return False
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == APPROVAL_PENDING)
                .values(
                    status=status,
                    decided_by=decided_by,
                    decided_at=func.now(),
                    note=note or "",
                )
            )
            await session.commit()
            return (result.rowcount or 0) == 1
    except Exception as exc:
        _fuse_off()
        log.warning("写审批结论失败：%s", exc)
        return False


async def revert_approval(approval_id: int) -> bool:
    """把已批准的审批项退回 pending（重新入队失败时用）。

    为什么需要回滚：状态机走到"已批准 + 任务 queued"之后，如果入队其实没成功，
    任务会永远挂着 queued 没人跑，而待审批项已经是 approved——
    用户既看不到审批卡片（没法再点一次），也等不到结果。这是最难受的一种死法：
    **界面上什么都没错，就是不动**。所以入队失败必须把审批项也退回去。

    只退 approved → pending：如果它已经是 rejected/cancelled（理论上不会发生，
    但并发下可能），不能把它改回 pending——那会让一条已经否决的动作重新变成"待批准"。
    """
    if not db_usable():
        return False
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == APPROVAL_APPROVED)
                .values(status=APPROVAL_PENDING, decided_by=None, decided_at=None, note="")
            )
            await session.commit()
            return (result.rowcount or 0) == 1
    except Exception as exc:
        _fuse_off()
        log.warning("回滚审批结论失败：%s", exc)
        return False


async def mark_interrupted() -> int:
    """启动补偿：上次进程崩了/被 kill 时，DB 里会留下 running。

    这些任务事实上不会再有人推进它，状态一直挂着会让人以为"还在跑"。
    改成 interrupted 是**诚实的失败**——比显示一个永远不动的 running 好。
    """
    if not db_usable():
        return 0
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                update(Task)
                # 只动 NEEDS_WORKER，**不含等待审批**：
                # 待审批的任务本来就没有 worker 在推它，进程重启也不会让它"丢"——
                # 如果把它一起改成 interrupted，用户刚收到审批通知就变成"任务已中断"。
                # 这个分组的差异正是 app/status.py 存在的理由。
                .where(Task.status.in_(NEEDS_WORKER))
                .values(status="interrupted", note="进程重启，任务已中断；详情见本地 trace 记录")
            )
            await session.commit()
            return result.rowcount or 0
    except Exception as exc:
        _fuse_off()
        log.warning("启动补偿失败：%s", exc)
        return 0


async def ping() -> bool:
    """探活：给启动日志用，让"库连不上"这件事在启动时就可见。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        _fuse_off()
        return False


# ---------------------------------------------------------------- 登录态（P5b）


def _credential_dict(row: Credential, *, with_ciphertext: bool = False) -> dict:
    """DB 行 → API 形状。

    为什么默认**不带密文**：列表接口只需要 id/站点/名字/过期时间。
    把密文也塞进响应等于让一串能解出 cookie 的令牌在网络和前端内存里多跑一遍，
    对调用方毫无用处，只是多一个泄露面。只有真正要用它的地方（注入浏览器）
    才用 with_ciphertext=True 取出来。
    """
    expired = False
    if row.expires_at is not None:
        # TIMESTAMPTZ 取回来带 UTC 时区，必须和 tz-aware 的 now() 比，
        # 否则抛 "can't subtract offset-naive and offset-aware datetimes"
        expired = row.expires_at <= datetime.now(tz=row.expires_at.tzinfo)
    data = {
        "id": row.id,
        "site": row.site,
        "label": row.label,
        "created_at": _fmt(row.created_at),
        "last_used_at": _fmt(row.last_used_at),
        "expires_at": _fmt(row.expires_at),
        # 算好给前端：让界面能直接显示"已过期，请重新保存"，而不是自己去比时间
        "expired": expired,
    }
    if with_ciphertext:
        data["ciphertext"] = row.ciphertext
    return data


async def save_credential(
    *,
    user_id: int,
    site: str,
    label: str,
    ciphertext: str,
    expires_at: datetime | None,
) -> int | None:
    """存一份登录态。同 (user_id, site) 已有则**覆盖**，返回 id。

    为什么是覆盖而不是新增一条：唯一索引定的是"一个用户一个站点一份"。
    允许存多份就得让用户回答"这次用哪份"——那个问题他们自己也答不上来，
    而且两份登录态里必有一份是旧的（cookie 早就过期了），只会制造混乱。
    重新保存 = "这份更新了"，语义比"再存一份"清楚得多。

    为什么用 select → insert/update 而不上 ON CONFLICT：
    ON CONFLICT 是 PostgreSQL 专属语法，而 repo 这一层还要能在 SQLite 上跑单测。
    并发下这里确实有窗口（两个请求同时发现"不存在"然后都 insert），
    但唯一索引会挡住第二个——它抛 IntegrityError，调用方拿到 None 并提示重试。
    **宁可偶尔失败一次，也不要用方言语法把可移植性焊死在这里。**
    """
    if not db_usable():
        return None
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(Credential).where(
                        Credential.user_id == user_id, Credential.site == site
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = Credential(
                    user_id=user_id,
                    site=site,
                    label=label,
                    ciphertext=ciphertext,
                    expires_at=expires_at,
                )
                session.add(row)
            else:
                row.label = label or row.label
                row.ciphertext = ciphertext
                row.expires_at = expires_at
            await session.commit()
            await session.refresh(row)
            return row.id
    except Exception as exc:
        _fuse_off()
        log.warning("保存登录态失败：%s", exc)
        return None


async def fetch_credentials(user_id: int) -> list[dict]:
    """列出某个用户保存的登录态（**不含密文**）。DB 不可用返回空列表。"""
    if not db_usable():
        return []
    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Credential)
                    .where(Credential.user_id == user_id)
                    .order_by(Credential.created_at.desc(), Credential.id.desc())
                )
            ).scalars().all()
            return [_credential_dict(r) for r in rows]
    except Exception as exc:
        _fuse_off()
        log.warning("列出登录态失败：%s", exc)
        return []


async def fetch_credential(
    credential_id: int, user_id: int | None = None
) -> dict | None:
    """按 id 取一份登录态，**带密文**（只有注入浏览器时用）。

    user_id 不为 None 时做归属过滤——这不是可选的礼貌，是权限边界：
    少了它，A 只要猜一个 id 就能用 B 的登录态跑任务，等于直接拿到 B 的账号。
    返回 None 和"不存在"不区分（和 assert_owned 同口径：不让探测存在性）。
    """
    if not db_usable():
        return None
    try:
        stmt = select(Credential).where(Credential.id == credential_id)
        if user_id is not None:
            stmt = stmt.where(Credential.user_id == user_id)
        async with SessionLocal() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _credential_dict(row, with_ciphertext=True) if row else None
    except Exception as exc:
        _fuse_off()
        log.warning("读取登录态失败：%s", exc)
        return None


async def delete_credential(credential_id: int, user_id: int) -> bool:
    """删掉一份登录态。归属不匹配等于删不到（返回 False → 上层报 404）。"""
    if not db_usable():
        return False
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                delete(Credential).where(
                    Credential.id == credential_id, Credential.user_id == user_id
                )
            )
            await session.commit()
            return (result.rowcount or 0) == 1
    except Exception as exc:
        _fuse_off()
        log.warning("删除登录态失败：%s", exc)
        return False


async def touch_credential(credential_id: int) -> None:
    """记一次"被任务用到了"。

    失败**不影响任务**：这是纯统计字段，写不进去最多让列表里的"最近使用"不准，
    不该因为它让一次已经起来的任务挂掉。
    """
    if not db_usable():
        return
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Credential)
                .where(Credential.id == credential_id)
                .values(last_used_at=func.now())
            )
            await session.commit()
    except Exception as exc:
        _fuse_off()
        log.warning("更新登录态使用时间失败：%s", exc)
