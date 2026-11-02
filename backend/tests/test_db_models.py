"""数据库模型与迁移的静态检查：**不需要连数据库**也能跑。

为什么专门写这几条：
1. 迁移脚本和 models.py 是两份定义，最容易出的问题是"改了一边忘了另一边"——
   症状是本地能跑、换台机器建不出表，且排查成本很高。
2. `task_steps` 不能加 UNIQUE(task_id, step_no) 这个结论是踩出来的
   （被拒绝的决策和重试后的决策共用同一个 step_no）。这种"看起来该加、其实不能加"
   的约束必须有测试守着，否则下一个人会好心地加回去。

验收方式用的是 alembic 的**离线模式**（`upgrade --sql`）：只生成 DDL，不连库。
所以这几个测试在没装 PostgreSQL 的机器上也能跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.db.models import Base

BACKEND = Path(__file__).resolve().parents[1]


def test_models_define_expected_tables():
    """表清单要稳定：多一张少一张都说明有人改了结构却没同步计划。

    2026-10-26 P4：新增 knowledge_nodes（数据闭环的知识节点），三张 → 四张。
    2026-10-26 P5a-2：新增 approvals（审批停等的记录与审计），四张 → 五张。
    2026-10-26 P5b：新增 credentials（加密后的登录态），五张 → 六张。
    """
    assert set(Base.metadata.tables) == {
        "users",
        "tasks",
        "task_steps",
        "knowledge_nodes",
        "approvals",
        "credentials",
    }


def test_task_steps_allows_repeated_step_no():
    """task_steps 不能有 UNIQUE(task_id, step_no)，且必须有 attempt 列。

    真实原因：workflow.py 的 _record_rejected() 会让"被拒绝的决策"和"重试后的决策"
    共用同一个 step_no（重试不推进步数）。加唯一约束 → 第一次拒绝就插不进去。
    attempt 列用来区分这两条，也是统计"被拒几次"的依据。
    """
    table = Base.metadata.tables["task_steps"]
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            assert set(constraint.columns.keys()) != {"task_id", "step_no"}, (
                "给 (task_id, step_no) 加了唯一约束：这会和 _record_rejected 冲突"
            )
    assert "attempt" in table.columns


def test_task_id_keeps_text_format():
    """tasks.id 必须仍是文本主键（沿用 20261024-122916-kaek3n）。

    换成数字主键会牵连截图路径、runs/ 目录名、前端 URL——无收益的连锁改动。
    """
    table = Base.metadata.tables["tasks"]
    pk_columns = list(table.primary_key.columns)
    assert len(pk_columns) == 1
    assert pk_columns[0].name == "id"
    assert "TEXT" in str(pk_columns[0].type).upper()


def test_migration_sql_contains_every_table():
    """迁移脚本要能独立生成 DDL，且覆盖全部表和索引。

    这条挡的是"改了 models 忘了改迁移"：两边不一致时，新机器上建出来的表
    和代码期望的不一样，报错还发生在很远的地方。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    sql = proc.stdout

    for table in ("users", "tasks", "task_steps", "knowledge_nodes", "approvals"):
        assert f"CREATE TABLE {table}" in sql, f"迁移里没有建 {table}"
    # 索引也校验：漏索引不会报错，只会让查询慢慢变慢，很难被发现
    for index in (
        "tasks_user_created",
        "tasks_status",
        "task_steps_task",
        "approvals_task_status",
    ):
        assert index in sql, f"迁移里没有建索引 {index}"
    # JSONB 是选 PostgreSQL 的主要理由之一（可索引），退化成 TEXT 就没意义了
    assert "JSONB" in sql
