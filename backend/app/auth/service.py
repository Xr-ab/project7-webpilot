"""认证纯逻辑：密码哈希、JWT 签发与校验。

两个刻意选择：
1. **bcrypt 只管哈希不管登录**：登录比对用 hmac.compare_digest 风格的常量时间
   比较（bcrypt.checkpw 内部就是），绝不自己写 `hash == input`——
   逐字节比较的耗时差可以被用来逐位猜密码（计时攻击）。
2. **JWT 用 pyjwt 最小面**：只签 HS256、只放 user_id 和过期时间两个 claim。
   不放 email/role——payload 里的每一样东西都是"签名保护但内容可见"的，
   放敏感信息等于明文广播（base64 解一下就能看）。
"""
from __future__ import annotations

import time
from typing import Any

import bcrypt
import jwt

from app.config import settings

# JWT 标准注册claim：sub（主体=用户id）、exp（过期）。
# 前缀只是可读性：万一以后加别的签发方，能一眼认出哪些字段是我们自己的约定
_ISSUER = "webpilot"


class AuthError(ValueError):
    """认证失败的统一异常。HTTP 层把它翻译成 401。"""


def hash_password(plain: str) -> str:
    """明文 → bcrypt 哈希。数据库里永远只存这个，明文密码用完即丢。

    bcrypt 自带盐（哈希串里包含），不用我们另外生成、另外存。
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。库里的哈希格式不对（空值/历史脏数据）一律算失败，不抛异常。"""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_token(user_id: int) -> str:
    """给用户签一个 JWT。内容只有"你是谁 + 用到什么时候"。"""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),  # 标准要求 sub 是字符串
        "iat": now,
        "exp": now + settings.token_expire_minutes * 60,
        "iss": _ISSUER,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_token(token: str) -> int:
    """校验 JWT，返回 user_id。任何一种不合法都统一抛 AuthError。

    为什么不区分"过期"和"伪造"：对调用方来说都是"这个 token 不能用"，
    区分只会给攻击者提供探测信息（比如拿到"这个用户的 token 还有效"的确认）。
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"token 无效：{type(exc).__name__}") from exc

    try:
        return int(payload.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise AuthError("token 里没有合法的用户标识") from exc
