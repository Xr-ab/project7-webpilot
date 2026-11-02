"""数据库连接：引擎 + 会话工厂 + FastAPI 依赖。

它在项目里的角色：
- 全项目只有这一处创建 engine。别处要数据库，要么拿这里导出的工厂，
  要么走 `get_session` 依赖 —— 多一个 engine 就多一个连接池，迟早出问题。
- M1 阶段这里只被 alembic 和后续 M2 的写入逻辑用到，**业务读路径还没接**。
  所以现在即使数据库没起，应用也能正常跑（`create_async_engine` 是懒连接的，
  不建连接就不会失败）。

一个刻意的选择：不在这里做 `Base.metadata.create_all()`。
建表只走 alembic —— 两套建表方式并存的结果一定是"本地手建的表和迁移文件不一致"，
而这种不一致最难查（本地能跑、新机器跑不起来）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# pool_pre_ping：连接空闲太久会被数据库单方面断掉，用之前先探活。
# 不加的话，长任务跑一半突然报 "connection closed"，而且报在建连接而不是用连接时，很难归因。
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    # echo=True 会把每条 SQL 打出来，调 SQL 时开、平时关
    echo=False,
)

# expire_on_commit=False：提交后对象还能读属性（否则会触发一次隐式刷新，
# 在 async 环境下这种隐式 IO 会直接抛 MissingGreenlet）
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个会话，请求结束自动关闭。

    为什么用 yield 而不是让调用方自己 close：
    依赖注入的收尾代码一定执行（哪怕路由里抛异常），比手工关可靠。
    """
    async with SessionLocal() as session:
        yield session
