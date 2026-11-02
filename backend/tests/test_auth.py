"""M4 认证与隔离测试。

分两层：
- 纯逻辑（service.py / deps.assert_owned）：不需要数据库，永远跑
- 注册/登录全链路：需要真 PostgreSQL，没起库自动跳过（与 test_stats 同策略）

隔离规则是本次测试的重点（验收原文：A 用 B 的 task_id → 404；不带 token → 401）。
路由层的 401 由 FastAPI 依赖产生（不依赖我们的代码分支），所以这里重点测
assert_owned 的 404 规则和 service 的密码/token 逻辑。
"""
from __future__ import annotations

import socket
import time as time_mod
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.engine.url import make_url

from app.auth import service
from app.auth.deps import assert_owned
from app.config import settings
from app.db.session import engine


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """每个测试前清空连接池（与 test_stats / test_knowledge 同款）。

    为什么必须：pytest-asyncio 给每个测试新建事件循环，而引擎连接池是全局单例；
    上一个测试留下的连接绑在旧循环上，下一个测试复用就会在 asyncpg 收尾时抛
    "Event loop is closed"。生产进程只有一个循环，不需要这么做。
    """
    await engine.dispose()
    yield


# ---------------------------------------------------------------- 纯逻辑


def test_password_hash_roundtrip():
    hashed = service.hash_password("s3cret-密码")
    assert hashed != "s3cret-密码"          # 明文绝不落库
    assert hashed.startswith("$2")          # bcrypt 标准前缀
    assert service.verify_password("s3cret-密码", hashed)


def test_verify_wrong_password():
    hashed = service.hash_password("correct")
    assert not service.verify_password("wrong", hashed)
    # 脏数据不抛异常，一律算失败
    assert not service.verify_password("correct", "")
    assert not service.verify_password("", hashed)


def test_token_roundtrip():
    token = service.issue_token(42)
    assert service.verify_token(token) == 42


def test_token_garbage_and_expired():
    with pytest.raises(service.AuthError):
        service.verify_token("not-a-jwt")
    # 过期 token：手工签一个 exp 在过去的
    import jwt as pyjwt

    expired = pyjwt.encode(
        {"sub": "1", "exp": int(time_mod.time()) - 10, "iss": "webpilot"},
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(service.AuthError):
        service.verify_token(expired)
    # 用错误密钥签的：同样拒绝
    forged = pyjwt.encode({"sub": "1", "exp": int(time_mod.time()) + 600}, "wrong-key", algorithm="HS256")
    with pytest.raises(service.AuthError):
        service.verify_token(forged)


def test_assert_owned_rules():
    """隔离判定四条规则（验收：别人的 → 404；没登录历史任务 → 404；关认证 → 放行）。"""
    mine = {"user_id": 1, "status": "success"}
    other = {"user_id": 2, "status": "success"}
    legacy = {"status": "success"}  # M4 之前的老 JSON 任务，meta 里没有 user_id
    me = SimpleNamespace(id=1)

    # 本人的任务：原样返回
    assert assert_owned(mine, me) == mine
    # 别人的任务 → 404（不是 403：403 会泄露"任务存在"这个事实）
    with pytest.raises(HTTPException) as exc:
        assert_owned(other, me)
    assert exc.value.status_code == 404
    # 无主历史任务对登录用户同样不可见
    with pytest.raises(HTTPException):
        assert_owned(legacy, me)
    # 认证关闭（user=None）：全放行，M3 行为
    assert assert_owned(other, None) == other
    assert assert_owned(legacy, None) == legacy


def test_assert_owned_missing_task_is_404():
    """任务根本不存在 → 404，而不是 200 + 空 dict。

    P5a-2 补审批接口时发现的老问题：原来 `return meta or {}` 把 None 吞成 {}，
    于是调用方那句 `if meta is None: raise 404` 永远执行不到，
    GET 一个乱写的 task_id 会返回 200 和空对象。行为上没越权，
    但前端拿到 {} 只能渲染空白页，说不清是"任务不存在"还是"数据丢了"。
    """
    me = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as exc:
        assert_owned(None, me)
    assert exc.value.status_code == 404
    # 认证关闭时同样要 404：任务不存在跟身份无关，这个结论不该随开关漂
    with pytest.raises(HTTPException) as exc:
        assert_owned(None, None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 真库全链路


def _db_reachable() -> bool:
    url = make_url(settings.database_url)
    host = url.host or "127.0.0.1"
    port = url.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _db_reachable(), reason="PostgreSQL 未启动")
async def test_register_login_flow():
    """注册 → 拿 token → 登录同账号 → 同一个 user_id。用完清理测试账号。"""
    from app.api.auth import Credentials, login, register
    from app.db.models import User
    from app.db.session import SessionLocal
    from sqlalchemy import delete

    stamp = str(int(time_mod.time() * 1000))[-9:]
    email = f"m4-test-{stamp}@webpilot.dev"
    password = "passw0rd-测试"

    try:
        reg = await register(Credentials(email=email, password=password))
        assert reg["email"] == email and reg["token"]

        # 签出的 token 必须能换回同一个 user_id
        assert service.verify_token(reg["token"]) == reg["user_id"]

        again = await login(Credentials(email=email, password=password))
        assert again["user_id"] == reg["user_id"]

        # 大小写不敏感：注册时的邮箱在登录时大小写混用也能登上
        mixed = await login(Credentials(email=email.upper(), password=password))
        assert mixed["user_id"] == reg["user_id"]

        # 隔离端到端：用另一个用户的身份访问本任务的 meta → 404
        other = SimpleNamespace(id=reg["user_id"] + 999999)
        with pytest.raises(HTTPException) as exc:
            assert_owned({"user_id": reg["user_id"]}, other)
        assert exc.value.status_code == 404
    finally:
        from sqlalchemy import select as _select

        async with SessionLocal() as session:
            row = (
                await session.execute(_select(User).where(User.email == email))
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()


@pytest.mark.skipif(not _db_reachable(), reason="PostgreSQL 未启动")
async def test_register_duplicate_and_login_wrong_password():
    from fastapi import HTTPException as _HTTPException

    from app.api.auth import Credentials, login, register

    stamp = str(int(time_mod.time() * 1000))[-9:]
    email = f"m4-test-{stamp}@webpilot.dev"
    try:
        await register(Credentials(email=email, password="password123"))
        # 重复注册 → 409
        with pytest.raises(_HTTPException) as dup:
            await register(Credentials(email=email, password="password123"))
        assert dup.value.status_code == 409
        # 错密码登录 → 401
        with pytest.raises(_HTTPException) as bad:
            await login(Credentials(email=email, password="wrong-password"))
        assert bad.value.status_code == 401
        # 不存在的用户登录 → 同样 401（不区分"没注册"和"密码错"，防枚举）
        with pytest.raises(_HTTPException) as ghost:
            await login(Credentials(email=f"ghost-{stamp}@webpilot.dev", password="whatever123"))
        assert ghost.value.status_code == 401
    finally:
        from sqlalchemy import delete as _delete, select as _select

        from app.db.models import User as _User
        from app.db.session import SessionLocal as _SL

        async with _SL() as session:
            row = (
                await session.execute(_select(_User).where(_User.email == email))
            ).scalar_one_or_none()
            if row is not None:
                await session.execute(_delete(_User).where(_User.id == row.id))
                await session.commit()


# ---------------------------------------------------------------- 登录限流（P5 收尾补 M4 遗留）


def _redis_reachable() -> bool:
    url = make_url(settings.redis_url)
    try:
        with socket.create_connection((url.host or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


redis_required = pytest.mark.skipif(
    not _redis_reachable(), reason="Redis 未启动；用 docker 起 6380 的实例后再跑"
)


async def _clear_failures(email: str) -> None:
    """清掉这个 email 的失败计数，测试互不污染（也防上次跑挂留下脏键）。"""
    from app.security import ratelimit as rl

    client = await rl._client()
    await client.delete(rl._key(email))


@redis_required
async def test_login_lockout_after_max_failures(monkeypatch):
    """同一 email 连续失败到上限 → 之后一律 429，**对密码都不看**。

    为什么 429 那次特意用正确密码：锁定的对象是"继续尝试"这个行为，
    不是"密码对不对"——如果持正确密码还能登进去，撞库者试到真密码
    那一刻就赢了，限流等于白做。
    """
    from app.api.auth import Credentials, login, register
    from app.db.models import User
    from app.db.session import SessionLocal
    from app.security import ratelimit
    from sqlalchemy import delete

    # pytest-asyncio 每个测试新事件循环，连接池是全局单例——必须重建
    #（理由同文件顶部 _fresh_pool 对 engine.dispose 的说明）
    monkeypatch.setattr(ratelimit, "_pool", None)

    stamp = str(int(time_mod.time() * 1000))[-9:]
    email = f"lock-test-{stamp}@webpilot.dev"
    password = "passw0rd-测试"

    try:
        await register(Credentials(email=email, password=password))

        # 上限之内的失败全部 401（还没有锁）
        for i in range(settings.login_max_failures - 1):
            with pytest.raises(HTTPException) as exc:
                await login(Credentials(email=email, password="wrong-password"))
            assert exc.value.status_code == 401, f"第 {i + 1} 次失败应是 401"

        # 这次失败把计数顶到上限：本次仍如实报 401
        with pytest.raises(HTTPException) as exc:
            await login(Credentials(email=email, password="wrong-password"))
        assert exc.value.status_code == 401

        # 顶到上限之后，正确密码也 429
        with pytest.raises(HTTPException) as exc:
            await login(Credentials(email=email, password=password))
        assert exc.value.status_code == 429
    finally:
        await _clear_failures(email)
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email == email))
            await session.commit()


@redis_required
async def test_login_success_clears_failure_count(monkeypatch):
    """失败几次 → 成功登录 → 计数清零：之后还能再错满一轮才锁。

    不做这一步的话，"手滑输错 2 次、登进来、下次又手滑 3 次"的正常人
    会在毫无预警的情况下被锁在门外——限流不能惩罚已经证明身份的人。
    """
    from app.api.auth import Credentials, login, register
    from app.db.models import User
    from app.db.session import SessionLocal
    from app.security import ratelimit
    from sqlalchemy import delete

    monkeypatch.setattr(ratelimit, "_pool", None)

    stamp = str(int(time_mod.time() * 1000))[-9:]
    email = f"lock-clear-{stamp}@webpilot.dev"
    password = "passw0rd-测试"

    try:
        await register(Credentials(email=email, password=password))

        for _ in range(2):
            with pytest.raises(HTTPException):
                await login(Credentials(email=email, password="wrong-password"))

        ok = await login(Credentials(email=email, password=password))
        assert ok["token"]

        # 清零生效：再错满"上限-1"次依然全是 401，没有 429
        for _ in range(settings.login_max_failures - 1):
            with pytest.raises(HTTPException) as exc:
                await login(Credentials(email=email, password="wrong-password"))
            assert exc.value.status_code == 401
    finally:
        await _clear_failures(email)
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email == email))
            await session.commit()


async def test_ratelimit_fails_open_when_redis_down(monkeypatch):
    """Redis 挂了 → 全部放行且不抛异常。

    这是 fail-open 设计的守门测试：如果有人把 except 改成了 raise，
    Redis 一抖全体用户登不进来——那比少一层防爆破严重得多。
    不需要真 Redis：直接把客户端工厂换成必炸的假货。
    """
    from app.security import ratelimit

    async def broken_client():
        raise ConnectionError("redis down")

    monkeypatch.setattr(ratelimit, "_client", broken_client)

    assert await ratelimit.blocked("anyone@webpilot.dev") is False
    # 两个写路径同样不该抛——抛了 login 就 500 了
    await ratelimit.record_failure("anyone@webpilot.dev")
    await ratelimit.clear("anyone@webpilot.dev")
