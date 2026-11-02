"""数据库集成测试：需要真实 PostgreSQL 才跑，没起库时**自动跳过**。

为什么要有一条真连库的测试：
`test_db_models.py` 只做静态检查（DDL 里有没有 table/index）。它能证明"迁移写得对"，
但证明不了"ORM 模型和建出来的表对得上"。这两者出现偏差时的症状是——
迁移跑通了、代码一写入就报 `column "xxx" does not exist`，而且报在很远的地方。

跳过策略：连不上库 或 没执行过迁移，都 skip（不 fail）。
理由：这个项目的定位是"任何人 clone 下来不装数据库也能跑单测"，
数据库是 P3 才引入的可选依赖。CI 上要跑它，起个 pg 容器就有了。
"""
from __future__ import annotations

import socket
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url

from app.config import settings
from app.db.models import Task, TaskStep, User
from app.db.session import SessionLocal, engine


def _db_reachable() -> bool:
    """按配置里的连接串探一下端口。用 socket 而不是连库，避免引入连接超时。"""
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
    reason="PostgreSQL 未启动；用 P3-实施计划.md 里的 docker run 命令起一个",
)


async def _migrated() -> bool:
    """迁移执行过没有。没执行就 skip，而不是让测试报一堆表不存在。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1 FROM alembic_version"))
        return True
    except Exception:
        return False


async def test_orm_matches_schema_and_allows_repeated_step_no():
    """写入-读回一遍，并验证同一 step_no 可以有多条（靠 attempt 区分）。

    这条测试挡的是两件事：
    1. ORM 模型与 DDL 漂移（字段名/类型对不上，写入时才炸）
    2. 有人给 (task_id, step_no) 加回唯一约束 —— 那会让被拒绝的决策插不进去
    """
    if not await _migrated():
        pytest.skip("数据库里没有 alembic_version，先执行 alembic upgrade head")

    suffix = uuid.uuid4().hex[:8]
    email = f"it-{suffix}@test.local"
    task_id = f"it-{suffix}"

    async with SessionLocal() as session:
        try:
            user = User(email=email, password_hash="not-a-real-hash")
            session.add(user)
            await session.flush()

            task = Task(
                id=task_id,
                user_id=user.id,
                goal="集成测试任务",
                start_url="http://127.0.0.1:8000/testsite/index.html",
                status="success",
                model=settings.llm_model,
                plan=["第一步", "第二步"],
                progress=[1],
                steps=1,
                prompt_tokens=100,
                completion_tokens=20,
            )
            session.add(task)
            await session.flush()

            # 同一个 step_no 两条：第一条被拒绝、第二条重试成功
            session.add(TaskStep(task_id=task_id, step_no=1, attempt=1, ok=False,
                                 error="e2 是 button，不能往它里面输入文字"))
            session.add(TaskStep(task_id=task_id, step_no=1, attempt=2, ok=True,
                                 action="click e2"))
            await session.commit()

            # ---- 读回，确认 JSONB 往返正常 ----
            back = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
            assert back.plan == ["第一步", "第二步"]
            assert back.progress == [1]
            assert back.status == "success"

            steps = (
                await session.execute(
                    select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.attempt)
                )
            ).scalars().all()
            assert [(s.step_no, s.attempt, s.ok) for s in steps] == [(1, 1, False), (1, 2, True)]

            # ---- 级联删除：删任务要把它的步骤一起带走 ----
            await session.delete(back)
            await session.commit()
            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM task_steps WHERE task_id = :tid"), {"tid": task_id}
                )
            ).scalar()
            assert remaining == 0
        finally:
            # 无论断言是否通过都要清干净，避免污染开发库
            await session.rollback()
            await session.execute(text("DELETE FROM tasks WHERE id = :tid"), {"tid": task_id})
            await session.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
            await session.commit()


async def test_engine_is_disposed_safely():
    """收尾测试：释放连接池。

    放在最后一条的原因：pytest 在同一进程里跑完所有用例，
    如果不 dispose，进程退出时可能留下未关闭的连接（日志里会出现
    "Event loop is closed" 之类与业务无关的噪音）。
    """
    await engine.dispose()
