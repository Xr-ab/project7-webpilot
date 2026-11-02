"""P5b：新增 credentials 表（登录态加密存储）

Revision ID: 0005_credentials
Revises: 0004_approvals
Create Date: 2026-10-26

为什么现在要建这张表：
P5a 收官时发现一个既有问题——`approvals.action` 存的是 `Action.model_dump()` 全量字段，
模型给密码框 `type` 时那个 `text` 就是**明文密码**，于是密码进了审批表。
这条泄露路径的根治办法不是"打码"，而是让 Agent **不需要在任务里填密码**：
先单独存一份登录态（storage_state），任务开局直接注入，密码根本不出现。

⚠️ 遵守"迁移只追加"：不动 0001-0004。
这条迁移不涉及历史数据回填（新表）。

字段设计说明见 app/db/models.py 的 Credential 类注释；
加密方案见 app/security/vault.py。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_credentials"
down_revision = "0004_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("site", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        # 整块 Fernet 密文。为什么不是 JSONB：密文是给程序解的不透明字符串，
        # 永远不会按内容查询，用 Text 最省（JSONB 还要校验合法性）
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # 用户注销 → 登录态跟着删。和 approvals.decided_by 的 SET NULL 刻意不同：
        # 审批记录是审计证据必须留，登录态是可用的凭证，主人没了必须清掉
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    # 唯一索引同时承担两件事：查"我的登录态"（按 user_id）和"同站只能有一份"
    op.create_index(
        "credentials_user_site", "credentials", ["user_id", "site"], unique=True
    )


def downgrade() -> None:
    op.drop_index("credentials_user_site", table_name="credentials")
    op.drop_table("credentials")
