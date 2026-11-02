"""初始表结构：users / tasks / task_steps

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-24

为什么手写而不是 autogenerate：autogenerate 要先连上库，而 M1 的验收标准是
"空库上能跑通"。手写之后用 `--sql` 离线生成 DDL 就能核对，不依赖数据库存在。

与 `app/db/models.py` 的对应关系必须一一对齐——两边不一致时以 models 为准，
然后重新生成这一份（表结构的单一真相是 models.py，迁移只是它的历史快照）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="users_email_key"),
    )

    op.create_table(
        "tasks",
        # 主键沿用现有 task_id 格式，不是自增数字
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("start_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_s", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="tasks_user_id_fkey"),
    )
    op.create_index("tasks_user_created", "tasks", ["user_id", "created_at"])
    op.create_index("tasks_status", "tasks", ["status"])

    op.create_table(
        "task_steps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        # 不能加 UNIQUE(task_id, step_no)：被拒绝的决策和重试后的决策共用同一个 step_no
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("thought", sa.Text(), nullable=False, server_default=""),
        sa.Column("action", sa.Text(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elements_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("screenshot", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="task_steps_task_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index("task_steps_task", "task_steps", ["task_id", "step_no"])


def downgrade() -> None:
    # 反过来删：先删有外键的那张
    op.drop_index("task_steps_task", table_name="task_steps")
    op.drop_table("task_steps")
    op.drop_index("tasks_status", table_name="tasks")
    op.drop_index("tasks_user_created", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("users")
