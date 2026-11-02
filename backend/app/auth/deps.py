"""FastAPI 依赖：把 HTTP 请求翻译成"当前登录的用户"。"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.auth.service import AuthError, verify_token
from app.config import settings
from app.db.models import User
from app.db.session import SessionLocal

# auto_error=False：token 缺失时我们自己抛 401（带中文说明），
# 依赖库默认会抛 403，语义不对——"没带凭证"是 401 Unauthorized
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User | None:
    """路由依赖：`user = Depends(get_current_user)` 一行接入。

    返回值约定：
    - 认证开启：返回登录用户（token 无效/缺失 → 401）
    - 认证关闭（AUTH_ENABLED=false）：返回 **None**——调用方把 user_id=None
      传下去，repo 层自会挂到占位用户（P3-M3 的行为）。
      这里刻意不查库：DB 挂掉 + 认证关闭时，任务必须照常能提交
      （隔离是增强项，不能比队列还硬）。
    """
    if not settings.auth_enabled:
        return None

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="未登录：请在 Authorization 头带上 Bearer token",
        )
    try:
        user_id = verify_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if user is None:
        # token 合法但用户没了（被删库）：等同于凭证失效
        raise HTTPException(status_code=401, detail="token 对应的用户不存在")
    return user


def assert_owned(meta: dict | None, user: User | None) -> dict:
    """任务归属校验（M4 隔离的唯一判定点）。

    规则：
    - meta 为 None → 404（任务真不存在）**由这里直接抛**
    - 认证关闭（user is None）→ 全部放行（M3 行为：本地单人模式）
    - 认证开启：meta 里没有 user_id（历史 JSON 任务，无主时代）或
      user_id 对不上 → 404。用 404 不用 403：403 等于承认
      "这个任务存在，只是不是你的"，会泄露 task_id 的存在性。

    为什么在 API 层比对而不是让 get_task 传 user_id 下钻：
    get_task 有三个数据来源（内存 / DB / JSON），内存那条路是纯 dict 查找，
    没有 SQL 可以挂 where——统一在出口比对 meta["user_id"]，
    三条来源一条规则，不会出现"DB 路径隔离了、内存路径漏了"的错位。

    ⚠️ 修过一个语义漏洞（P5a-2 补审批接口时发现）：
    原来写的是 `if meta is None or user is None: return meta or {}`，
    于是"任务不存在"被吞成空 dict 返回给调用方，`GET /tasks/{乱写的id}`
    实际是 **200 + `{}`** 而不是 404。调用方那句 `if meta is None: raise 404`
    永远执行不到（`{}` 不是 None）。行为上不算越权，但语义是错的：
    前端拿到 `{}` 会渲染一个空白详情页，而不是"任务不存在"。
    现在"不存在"在这里就地报 404，调用方不需要再判一次。
    """
    if meta is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if user is None:
        return meta
    if meta.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return meta
