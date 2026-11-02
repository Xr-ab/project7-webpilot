"""arq worker：真正执行任务的进程。

启动方式（必须和 API 分开跑）：
    cd backend && <venv python> -m arq app.worker.WorkerSettings

它在项目里的角色：
- API 进程只负责接请求、入队、查询；执行在这里。
- 好处是并发有上限（`max_jobs`）、API 重启不会杀掉正在跑的任务、
  以后可以多开几个 worker 横向扩。

**这里最关键的设计是"浏览器归谁管"**：

    worker 进程启动 → launch 一个 Chromium（ctx["browser"]）
    每个任务 → 在这个 Chromium 上开一个新的 Context → 跑完关掉 Context

为什么不每个任务 launch 一个（P1 的做法）：
launch 冷启动 1~4 秒，而任务本身常常只要 2~10 秒，冷启动占了可观比例。
共用 Browser 之后启动只发生一次；隔离性仍然成立，因为 cookie / localStorage /
缓存都在 Context 级别。

⚠️ 这条路的唯一真风险：**谁关了 Browser 谁就把同进程里别人的任务干死**。
所以 BrowserSession 用 `browser=` 参数显式表达"这是借来的，close 时别关"
（见 browser/session.py 的 close()）。这里也绝不在任务里 close 它。
"""
from __future__ import annotations

import logging

from arq.connections import RedisSettings
from playwright.async_api import async_playwright

from app.browser.session import _proxy_config
from app.config import settings
from app.db import repo
from app.runner import task_runner

log = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    """worker 启动：起 Playwright + 一个共享 Chromium。

    放在 startup 而不是每个任务里，就是为了把冷启动成本摊到整个进程寿命上。
    """
    ctx["pw"] = await async_playwright().start()

    launch_kwargs: dict = {"headless": True}
    proxy = _proxy_config()
    if proxy is not None:
        launch_kwargs["proxy"] = proxy
    ctx["browser"] = await ctx["pw"].chromium.launch(**launch_kwargs)

    # 顺带做一次启动补偿：上次 worker 被 kill 时留下的 running 任务，
    # 事实上不会再有人推进它了。改成 interrupted 比显示一个永远不动的 running 诚实。
    if await repo.ping():
        fixed = await repo.mark_interrupted()
        log.info("worker 启动；补偿清理了 %d 个中断任务", fixed)
    else:
        log.warning("数据库连不上：任务照常执行，但状态只落 JSON")


async def shutdown(ctx: dict) -> None:
    """worker 退出：关共享浏览器。

    顺序反过来（先关 pw 再关 browser）会留下僵尸 Chromium 进程 ——
    playwright 的 stop() 依赖 browser 还在它自己的连接里。
    """
    browser = ctx.get("browser")
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    pw = ctx.get("pw")
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


async def run_task(ctx: dict, task_id: str, goal: str, start_url: str) -> str:
    """arq 的任务函数。名字必须和 `submit` 里 enqueue_job 的字符串一致。

    ⚠️ 为什么这里要包一层 try（P5b 加的）：
    `execute_task` 内部已经有兜底 try，正常不该有异常逃到这一层。但"正常"是靠人守的——
    P5b 就把一段准备代码错放到 `execute_task` 的 try 外面，异常一路冲到这里，
    被 arq 记成 job failed，而 **arq 不知道我们库里有个任务**，没人把 running 改成 error。
    任务永久停在 running：前端无限转圈、事后零线索。

    ⚠️ 为什么捕获后**不重新抛出**：
    raise 出去 arq 可能重试这个 job（取决于 max_tries），而任务执行是有副作用的——
    重试意味着「提交」按钮被点第二次、表单被填第二遍。
    一个"状态不准"的问题不值得用"动作执行两遍"去换。
    所以：写终态 → 记日志 → 正常返回。
    """
    log.info("开始执行任务 %s: %s", task_id, goal[:40])
    try:
        # 把共享浏览器传进去：execute_task 内部会为这个任务开一个独立 Context
        await task_runner.execute_task(task_id, goal, start_url, browser=ctx["browser"])
    except Exception as exc:
        # log.exception 必须在这里调（在 except 块里），堆栈才抓得到；
        # mark_crashed 内部是另一个协程，sys.exc_info() 到那里已经空了
        log.exception("任务 %s 的 job 抛出了未捕获异常", task_id)
        await task_runner.mark_crashed(task_id, exc)
    log.info("任务结束 %s", task_id)
    return task_id


async def resume_task(ctx: dict, task_id: str, approval_id: int) -> str:
    """续跑任务函数（P5a-3）。名字必须和 `_enqueue_resume` 里的字符串一致。

    为什么 job 只带 approval_id 而不带动作本体：
    - 动作、上下文、暂停页地址都在 approvals 表里，redis 只传一个整数，
      不会因为任务上下文变大而撞上 arq 的序列化/体积边界；
    - 更重要的是**单一真相**：执行的动作必须来自库里那条被批准的记录。
      如果把动作塞进 job 参数，库里的记录被改过之后，job 里那份就是旧的——
      执行的和审计记的不是一回事，审批就失去意义了。
    """
    log.info("续跑任务 %s（审批项 %s）", task_id, approval_id)
    try:
        await task_runner.execute_resume(task_id, approval_id, browser=ctx["browser"])
    except Exception as exc:
        # 和 run_task 同一个理由。这里更要紧：续跑崩溃如果不兜底，
        # 任务会停在 queued（resume 先改状态再入队），而审批项已经是 approved ——
        # 界面显示"排队中"、库里显示"已批准"，两份真相互相矛盾
        log.exception("续跑任务 %s 的 job 抛出了未捕获异常", task_id)
        await task_runner.mark_crashed(task_id, exc)
    log.info("续跑结束 %s", task_id)
    return task_id


class WorkerSettings:
    """arq 的入口配置（`-m arq app.worker.WorkerSettings` 就是读这个类）。"""

    # 两个函数都要注册：只注册 run_task 的话，续跑 job 入队成功但永远没人消费——
    # 任务停在 queued，前端一直转圈，而日志里连报错都没有。
    functions = [run_task, resume_task]
    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # 并发上限：由用户拍板定 3（免费档 LLM 有并发限制，每个 Chromium 吃 200~400MB）
    max_jobs = settings.max_jobs
    # 一个任务最长 180s（config.task_timeout_seconds），job 超时留出余量
    job_timeout = settings.task_timeout_seconds + 60
    # 任务来回 polling 的间隔：不设的话空闲 worker 会一直轮询 redis
    poll_delay = 0.5
    # 结果保留 1 小时，够前端在任务结束后仍然查到（我们的真相在 DB，这里只是兜底）
    keep_result = 3600
