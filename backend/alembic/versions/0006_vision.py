"""P6a：task_steps 新增 vision 列（视觉兜底留痕）

Revision ID: 0006_vision
Revises: 0005_credentials
Create Date: 2026-10-28

为什么现在加这一列（一次真实踩坑，踩坑日志 #42）：

P6 计划里写的是"trace 行新增 vision 字段，回放器/M5 前端忽略未知字段——
P5a 已验证过这种兼容性"。这句话对 **trace.json** 成立（JSON 只增字段，读的人按 .get() 取），
但 trace 还有第二个落点：`task_steps` 表——它是**固定列**的。

A5 端到端第一跑的情形：任务成功、答案正确、runs/<id>/trace.json 里
vision.used=true 明明在，可是脚本查 `/api/v1/tasks/{id}/trace`（读 DB）
一条都看不到，两条断言红得莫名其妙。**磁盘上有、接口里没有**。

所以这条迁移不是"功能增强"，是把两种存储的字段登记补平：
以后凡是 trace 行上的新字段，都要问一句"DB 那一份呢"。

nullable=True：绝大多数步骤没有视觉信息（默认关 + 兜底才触发），
既要能表达"没看图"，也不想给老数据回填一个 {}（那会让统计把"老数据"
算成"看图失败"）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_vision"
down_revision = "0005_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_steps",
        sa.Column("vision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_steps", "vision")
