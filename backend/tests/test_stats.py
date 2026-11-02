"""统计接口测试（M5b）：需要真实 PostgreSQL，没起库时自动跳过。

为什么造数走 repo 全链路（create → running → insert_steps → finish），
而不是直接 INSERT：fetch_stats 的数字必须和"真实写入路径"产生的数据对得上。
finish_task 里那段 token 汇总逻辑（SUM task_steps 回写 tasks）只有走全链路才会执行。

对账策略：测试开始前记一个时刻 t0，造 3 个已知数字的任务，
然后 fetch_stats(since=t0) —— since 把库里已有的真实任务（如 eval_real 跑的）
全部排除在外，剩下的只有测试自己造的数，可以逐项手算对账。
造的数据用 stats-test- 前缀，teardown 删干净，不污染真库。
"""
from __future__ import annotations

import socket
import time as time_mod
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete
from sqlalchemy.engine.url import make_url

from app.api.stats import get_stats
from app.config import settings
from app.db import repo
from app.db.models import Task, TaskStep
from app.db.session import SessionLocal, engine

# 与 test_db_integration 相同的跳过策略：没起 PG 就 skip（数据库是可选依赖）
def _db_reachable() -> bool:
    url = make_url(settings.database_url)
    host = url.host or "127.0.0.1"
    port = url.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="PostgreSQL 未启动；用 docker 起 wp-postgres 容器后再跑",
)

_PREFIX = "stats-test-"


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """每个测试开始前清空连接池。

    为什么必须：pytest-asyncio 给每个测试新建事件循环，而引擎的连接池是
    全局单例——上一个测试留下的连接绑在旧循环上，下一个测试复用就会报
    "Event loop is closed"。dispose 之后本测试用新循环重新建连接。
    （只在测试里这么做；生产进程只有一个循环，不需要。）
    """
    await engine.dispose()
    yield


async def _make_task(
    task_id: str,
    *,
    status: str,
    steps: int,
    elapsed: float | None,
    goal: str | None = None,
) -> None:
    """用真实写入路径造一个任务。每步固定 prompt=100 / completion=20。"""
    ok = await repo.create_task(
        task_id=task_id,
        goal=goal or f"统计测试 {task_id}",
        start_url="http://127.0.0.1:8000/testsite/",
        model="glm-4-flash",
    )
    assert ok, "造数时写库失败——先确认 PG 容器和迁移都在"
    await repo.mark_running(task_id)
    if steps:
        rows = [
            {
                "step": i + 1,
                "url": "http://127.0.0.1:8000/testsite/",
                "thought": "t",
                "action": "scroll down",
                "ok": True,
                "error": None,
                "duration_ms": 100,
                "elements_total": 5,
                "screenshot": None,
                "llm_usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
            for i in range(steps)
        ]
        await repo.insert_steps(task_id, rows)
    await repo.finish_task(
        task_id,
        status=status,
        answer="答案" if status == "success" else "",
        note="",
        steps=steps,
        plan=[],
        progress=[],
        elapsed_s=elapsed,
    )


async def _cleanup(task_ids: list[str]) -> None:
    """把造的数据删干净。task_steps 有 CASCADE，但显式删两表不依赖库配置。"""
    async with SessionLocal() as session:
        await session.execute(delete(TaskStep).where(TaskStep.task_id.in_(task_ids)))
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        await session.commit()


async def test_fetch_stats_matches_hand_computed():
    """3 个已知任务（2 success + 1 loop），stats 数字必须和手算一致。"""
    # t0 往前拨 5 秒，吸收容器与主机之间的微小时钟差；
    # since 传 datetime（与 repo 签名一致，路由层的字符串解析在另外的路径测）
    since = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 5, tz=timezone.utc
    )

    ids = [f"{_PREFIX}t{i}" for i in (1, 2, 3)]
    try:
        # t1/t2 故意用同一个 goal：验证"分任务表"把同题多次尝试聚成一行
        await _make_task(ids[0], status="success", steps=2, elapsed=10.0, goal="统计测试-同题")
        await _make_task(ids[1], status="success", steps=4, elapsed=20.0, goal="统计测试-同题")
        await _make_task(ids[2], status="loop", steps=4, elapsed=30.0, goal="统计测试-单题")

        stats = await repo.fetch_stats(since=since)
        assert stats is not None, "fetch_stats 返回 None（DB 熔断？）"

        # ---- 逐项手算对账 ----
        assert stats["total"] == 3
        assert stats["unfinished"] == 0
        assert stats["success"] == 2
        assert stats["success_rate"] == 66.7          # 2/3
        assert stats["avg_steps"] == 3.33             # (2+4+4)/3
        # P50 of [10,20,30] = 20
        assert stats["elapsed_p50_s"] == 20.0
        # P95：线性插值 20 + 0.9*(30-20) = 29.0
        assert stats["elapsed_p95_s"] == 29.0
        # token：每步 100/20，共 10 步 → 1000 / 200
        assert stats["tokens"] == {"prompt": 1000, "completion": 200, "total": 1200}
        assert stats["status_distribution"] == {"success": 2, "loop": 1}

        # ---- 分任务表对账 ----
        by_goal = {row["goal"]: row for row in stats["by_goal"]}
        same = by_goal["统计测试-同题"]
        assert same["total"] == 2 and same["success"] == 2
        assert same["success_rate"] == 100.0
        assert same["avg_steps"] == 3.0               # (2+4)/2
        assert same["avg_elapsed_s"] == 15.0          # (10+20)/2
        solo = by_goal["统计测试-单题"]
        assert solo["total"] == 1 and solo["success"] == 0
        assert solo["success_rate"] == 0.0
    finally:
        await _cleanup(ids)


async def test_fetch_stats_excludes_unfinished():
    """只提交没跑完的任务（queued）不进分母——成功率不随查询时机抖动。"""
    since = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 5, tz=timezone.utc
    )

    done_id, queued_id = f"{_PREFIX}done", f"{_PREFIX}queued"
    try:
        await _make_task(done_id, status="success", steps=1, elapsed=5.0)
        # 只 create_task，不 finish：它应留在 queued，不进统计
        ok = await repo.create_task(
            task_id=queued_id,
            goal="统计测试 queued",
            start_url="http://127.0.0.1:8000/testsite/",
            model="glm-4-flash",
        )
        assert ok

        stats = await repo.fetch_stats(since=since)
        assert stats is not None
        assert stats["total"] == 1          # 只有 done 那个
        assert stats["unfinished"] == 1     # queued 单独计数
        assert stats["success_rate"] == 100.0
    finally:
        await _cleanup([done_id, queued_id])


async def test_stats_endpoint_degraded(monkeypatch):
    """DB 熔断时路由返回 available=false 而不是抛错（统计是增强功能）。"""
    # 把熔断开关拨到"未来 999 秒"：repo 里所有函数会直接短路返回 None
    monkeypatch.setattr(repo, "_db_down_until", time_mod.monotonic() + 999)
    out = await get_stats(since=None)
    assert out["available"] is False
    assert "note" in out
