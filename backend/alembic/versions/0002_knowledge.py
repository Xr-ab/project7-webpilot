"""P4 数据闭环：knowledge_nodes 表（含向量列）

Revision ID: 0002_knowledge
Revises: 0001_initial
Create Date: 2026-10-26

与 `app/db/models.py` 的 KnowledgeNode 一一对齐（表结构真相仍是 models.py）。

两个设计取舍：
1. **先 CREATE EXTENSION 再建表**：`vector` 是扩展类型，不装扩展建表会报
   `type "vector" does not exist`。IF NOT EXISTS 保证重复执行不炸；
   如果镜像里的 postgres 没装 pgvector 扩展文件，这行会失败——
   这是有意为之的 fail-fast：宁可迁移当场报错，也不要建出一个没有向量的"知识表"。
2. **暂不建向量索引**：几十~几百条数据顺序扫描（几毫秒）比维护索引划算；
   顺序扫描同时是精确解，小数据下比 HNSW 的近似解更准。
   数据过千条再加（届时 `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`，
   要求 pgvector ≥ 0.5）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0002_knowledge"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 扩展是库级对象：一个库装一次，之后所有表都能用 vector 类型
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_nodes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        # content 的 sha256：入库判重键。长文本不能直接建唯一索引（btree 有长度上限），
        # 所以用定长哈希代替
        sa.Column("content_hash", sa.Text(), nullable=False),
        # 维度必须与 models.EMBED_DIM 一致（bge-small-zh-v1.5 = 512）
        sa.Column("embedding", Vector(512), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="knowledge_task_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index("knowledge_task", "knowledge_nodes", ["task_id"])
    op.create_index("knowledge_source", "knowledge_nodes", ["source_type"])
    op.create_index("knowledge_hash", "knowledge_nodes", ["content_hash"])


def downgrade() -> None:
    op.drop_index("knowledge_hash", table_name="knowledge_nodes")
    op.drop_index("knowledge_source", table_name="knowledge_nodes")
    op.drop_index("knowledge_task", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
    # 不 DROP EXTENSION：扩展是库级共享对象，别的表可能也在用；
    # 回滚一张表不该动整个库的能力。
