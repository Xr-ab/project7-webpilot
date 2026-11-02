"""认证接口：注册 + 登录。就这两个，登录后的所有请求都靠 token 自证身份。

为什么注册/登录不分文件：都是"用 email+密码换身份"的一件事，
拆开反而要把 verify/hash 的调用横跨两个文件。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.service import hash_password, issue_token, verify_password
from app.config import settings
from app.db.models import User
from app.db.session import SessionLocal
from app.security import ratelimit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# 够用的 email 格式检查：有本地部分、有 @、有域名。
# 全面的 RFC 校验是出了名的复杂度黑洞，v0.1 这里拦住手滑就够了
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# bcrypt 有 72 字节输入上限，且太长的密码没有任何安全收益
_PASSWORD_MIN = 8
_PASSWORD_MAX = 64


class Credentials(BaseModel):
    """注册和登录共用一个形状：email + password。"""

    email: str = Field(max_length=120)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


def _validate(email: str, password: str) -> None:
    """注册时的字段校验。登录不做格式校验——错了就是"密码不对"，不泄露细节。"""
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="email 格式不对")
    if len(password) < _PASSWORD_MIN:
        raise HTTPException(status_code=400, detail=f"密码至少 {_PASSWORD_MIN} 位")


@router.post("/register", summary="注册（email + 密码），成功直接返回 token")
async def register(req: Credentials) -> dict:
    if not settings.auth_enabled:
        raise HTTPException(status_code=403, detail="认证已关闭（AUTH_ENABLED=false）")

    _validate(req.email, req.password)
    email = req.email.strip().lower()

    async with SessionLocal() as session:
        exists = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if exists is not None:
            # 409 Conflict：资源已存在。不用 400，语义更准
            raise HTTPException(status_code=409, detail="该 email 已注册，直接登录即可")
        user = User(email=email, password_hash=hash_password(req.password))
        session.add(user)
        await session.commit()
        await session.refresh(user)  # 拿自增 id

    return {"user_id": user.id, "email": user.email, "token": issue_token(user.id)}


@router.post("/login", summary="登录，返回 JWT token")
async def login(req: Credentials) -> dict:
    if not settings.auth_enabled:
        raise HTTPException(status_code=403, detail="认证已关闭（AUTH_ENABLED=false）")

    email = req.email.strip().lower()

    # 限流闸（M4 遗留的收尾）：这个 email 失败太多次就先拒之门外。
    # 429 而不是 401：问题不在"密码不对"，而在"当前状态不允许再试"，
    # 前端和攻击者都需要区分这两者——后者靠 401 继续猜没有意义。
    if await ratelimit.blocked(email):
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {settings.login_lockout_seconds // 60} 分钟后再试",
        )

    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

    # 统一报"email 或密码不对"：分开设的话，攻击者能借此枚举哪些 email 注册过
    if user is None or not verify_password(req.password, user.password_hash):
        # 失败要记账（包括"email 不存在"的尝试——统一处理避免枚举侧信道）
        await ratelimit.record_failure(email)
        raise HTTPException(status_code=401, detail="email 或密码不对")

    # 成功即翻篇：之前的失败记录不该影响已经证明了自己身份的人
    await ratelimit.clear(email)
    return {"user_id": user.id, "email": user.email, "token": issue_token(user.id)}
