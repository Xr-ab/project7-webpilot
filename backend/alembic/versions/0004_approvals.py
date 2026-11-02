"""P5a-2：新增 approvals 表（审批停等的落库与审计）

Revision ID: 0004_approvals
Revises: 0003_knowledge_owner
Create Date: 2026-10-26

为什么现在要建这张表：
P5a-1 把待审批信息只写进了 `runs/<task_id>/meta.json`，够前端渲染，
但那是"任务当前状态的快照"，回答不了审计问题——谁批的、批了几次、为什么拒。
这几个问题在 P5a-3（批准后续跑）会直接变成功能依赖：
续跑要知道"批的是哪一条动作"，拒绝要把理由回喂给模型。

⚠️ 遵守"迁移只追加"：不去动 0001-0003。
这条迁移不涉及历史数据回填（新表，历史任务本来就没有审批记录）。

字段设计说明见 app/db/models.py 的 Approval 类注释。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_approvals"
down_revision = "0003_knowledge_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        # 两个 JSON 列用 JSONB：要按里面的键查（比如将来"哪些 goal 最常触发审批"）
        sa.Column("action", JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot_url", sa.Text(), nullable=False),
        sa.Column("context", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # 审批人可为空：pending 时还没人批；用户注销后 SET NULL 保留记录
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        # server_default 而不是只在 Python 侧 default：这张表要能被手工 SQL 写入
        # （运维补数据/回滚排查），不能让 created_at 依赖 ORM
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # 任务删了审批记录跟着删：留下指向不存在任务的孤儿行，审计也没有意义
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        # 用户注销不删记录（审计要留痕），只把"谁批的"置空
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
    )
    # 最高频查询：这个任务有没有 pending 项。复合索引 (task_id, status) 一次覆盖
    op.create_index("approvals_task_status", "approvals", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("approvals_task_status", table_name="approvals")
    op.drop_table("approvals")
