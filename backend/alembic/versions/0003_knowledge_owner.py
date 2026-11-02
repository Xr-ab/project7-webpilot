"""M4 收尾：knowledge_nodes 增加采集者 user_id（只记录，不参与检索过滤）

Revision ID: 0003_knowledge_owner
Revises: 0002_knowledge
Create Date: 2026-10-26

⚠️ 这是**新增**迁移，不是改 0002。踩过的坑：直接编辑已执行过的迁移文件，
库里 alembic_version 仍指向旧版本，downgrade 时会拿新文件的语句去删旧结构，
报 "index knowledge_hash does not exist"。规则：迁移一旦在某个库上执行过，只能追加。

为什么 nullable：
- 历史节点是在这一列存在之前入库的，回填 owner 要按 task 反查，收益低
- 采集脚本、无主任务本来就可能没有明确 owner

为什么不设为 NOT NULL + 默认值：没有合理的默认值（挂到谁头上都是错的），
宁可留 NULL 表达"不知道是谁采集的"。

为什么**不加**检索过滤：P4 的定位是"知识跨任务、跨用户积累"，
按用户过滤会让新账号第一次提问什么都搜不到。这一列先留数据、留切换能力。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_knowledge_owner"
down_revision = "0002_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_nodes",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
    )
    # 只建普通索引，不建唯一约束：同一个人采集到多条相同内容时靠 content_hash 判重，
    # 不是靠 user_id 唯一。
    op.create_index("knowledge_owner", "knowledge_nodes", ["user_id"])
    # 外键单独加，命名与 models.py 的隐式命名保持可读一致
    op.create_foreign_key(
        "knowledge_user_id_fkey",
        "knowledge_nodes",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- 回填历史节点的采集者 ----
    # 为什么要写进迁移而不是"记得手工跑一次 SQL"：
    # 迁移的价值就是"任何环境执行一次就到位"。漏在这儿的回填，别人 clone 下来
    # 跑完迁移会得到一堆 user_id=NULL 的节点，然后疑惑"为什么只有新数据有 owner"。
    #
    # UPDATE ... FROM 是 PostgreSQL 的写法（不能用子查询相关更新的通用形式）：
    # 知识节点自己只有 task_id，owner 的唯一真相在 tasks 表上。
    op.execute(
        """
        UPDATE knowledge_nodes AS k
        SET user_id = t.user_id
        FROM tasks AS t
        WHERE k.task_id = t.id
          AND k.user_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("knowledge_user_id_fkey", "knowledge_nodes", type_="foreignkey")
    op.drop_index("knowledge_owner", table_name="knowledge_nodes")
    op.drop_column("knowledge_nodes", "user_id")
