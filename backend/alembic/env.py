"""alembic 运行时环境。

它在项目里的角色：把"迁移脚本"和"真实数据库"接起来。
两条路径：
- `alembic upgrade head`    → 连库执行（异步驱动 asyncpg）
- `alembic upgrade head --sql` → **不连库**，只把 DDL 打印出来

第二条很重要：本地没有数据库时也能验证"迁移脚本生成的 SQL 对不对"，
比"等数据库装好再试"早发现一半的错误。
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
# 必须 import models：alembic 是靠 Base.metadata 里的表定义工作的，
# 不 import 的话 metadata 是空的，autogenerate 会把所有表当成"需要删除"。
from app.db.models import Base

config = context.config

# 日志配置只在使用 ini 文件时生效（在单测里调用时可能没有 ini）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL 只有一处真相：app.config（也就是 .env）
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool：迁移是一次性动作，用完就断，不需要连接池
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async_migrations())
