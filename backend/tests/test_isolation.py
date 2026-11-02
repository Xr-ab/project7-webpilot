"""M4 收尾：隔离的完整性与媒体签名测试。

为什么要单独一个文件（test_auth.py 已经测过 assert_owned 的 404）：
test_auth 测的是"正门"——详情接口按归属过滤。这次补的是**旁路**：
`/runs` 静态目录原来不需要任何凭证，于是
    knowledge/ask → 别人的 task_id → /runs/<id>/meta.json
就绕过了正门的所有检查。安全上，一条未上锁的旁路等于没有正门。

分三层（都不需要起浏览器，跑得很快）：
1. signing 纯逻辑：签名/篡改/过期/缺参
2. media 路由：后缀白名单、路径穿越、验签、文件不存在
3. task 列表隔离：需要真 PostgreSQL，没起库自动跳过（同 test_stats 策略）
"""
from __future__ import annotations

import socket
import time as time_mod

import pytest
from fastapi import HTTPException
from sqlalchemy.engine.url import make_url

from app.api import media
from app.config import settings
from app.db import repo
from app.db.models import Task, User
from app.db.session import SessionLocal, engine
from app.storage import signing


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """清空连接池（与 test_stats / test_knowledge / test_auth 同款）。

    pytest-asyncio 每个测试新建事件循环，而引擎连接池是全局单例；
    上个测试留下的连接绑在旧循环上，复用时会抛 "Event loop is closed"。
    """
    await engine.dispose()
    yield


# ---------------------------------------------------------------- 1. 签名逻辑


def test_sign_and_verify_roundtrip():
    url = signing.sign_url("task-a", "step_1.png")
    # URL 里必须同时带过期时间和签名，否则前端拿到也用不了
    assert "?e=" in url and "&sig=" in url
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert signing.verify("task-a", "step_1.png", query["e"], query["sig"])


def test_verify_rejects_signature_moved_to_another_task():
    """签名的核心性质：不能"移植"。

    拿到 A 的截图签名后，改成 B 的 task_id 必须失效——
    否则泄露一张截图的地址就等于泄露整个 runs 目录的读权限。
    """
    url = signing.sign_url("task-a", "step_1.png")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert not signing.verify("task-b", "step_1.png", query["e"], query["sig"])


def test_verify_rejects_expired():
    url = signing.sign_url("task-a", "step_1.png", ttl=-1)  # 签发即过期
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert not signing.verify("task-a", "step_1.png", query["e"], query["sig"])


def test_verify_rejects_missing_or_malformed():
    assert not signing.verify("t", "a.png", None, None)
    assert not signing.verify("t", "a.png", "", "")
    # 非数字的 e 是常见的探测手法，必须返回 False 而不是抛异常（抛就成了 500）
    assert not signing.verify("t", "a.png", "abc", "deadbeef")


def test_signature_changes_with_filename():
    """不同文件必须签出不同签名，否则一张图的地址可以读整个目录。"""
    s1 = signing.sign_url("task-a", "step_1.png").split("sig=")[1]
    s2 = signing.sign_url("task-a", "step_2.png").split("sig=")[1]
    assert s1 != s2


def test_sign_uses_configured_ttl_time_window():
    """过期时间戳大致落在 now + 配置 TTL 附近（容差 5 秒，避免时钟抖动）。"""
    url = signing.sign_url("task-a", "step_1.png")
    expires = int(url.split("e=")[1].split("&")[0])
    expected = int(time_mod.time()) + settings.media_url_ttl_seconds
    assert abs(expires - expected) <= 5


# ---------------------------------------------------------------- 2. 媒体路由


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    """把 media 路由的 RUNS_ROOT 换成临时目录。

    为什么要 monkeypatch 而不是往真 runs/ 里写测试文件：
    真目录有 500+ 张截图，测试混进去之后分不清哪些是脏数据；
    临时目录测试结束自动消失，也不会误删真产物。
    """
    monkeypatch.setattr(media, "RUNS_ROOT", tmp_path)
    return tmp_path


async def test_media_refuses_json_even_when_signed(runs_root):
    """meta.json / trace.json 即使签名正确也一律不给。

    理由：任务全文只有一条合法出口 `GET /api/v1/tasks/{id}`（带归属校验）。
    在静态路径上再开一个出口，等于给隔离留了第二扇门。
    """
    (runs_root / "task-a").mkdir()
    (runs_root / "task-a" / "meta.json").write_text('{"answer": "泄露"}', encoding="utf-8")
    url = signing.sign_url("task-a", "meta.json")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))

    with pytest.raises(HTTPException) as exc:
        await media.get_media("task-a", "meta.json", query["e"], query["sig"])
    assert exc.value.status_code == 403


async def test_media_rejects_unsigned_request(runs_root):
    """原始漏洞的复现点：不带任何凭证访问截图 → 403（修复前是 200）。"""
    (runs_root / "task-a").mkdir()
    (runs_root / "task-a" / "step_1.png").write_bytes(b"fake-png")

    with pytest.raises(HTTPException) as exc:
        await media.get_media("task-a", "step_1.png", None, None)
    assert exc.value.status_code == 403


async def test_media_rejects_tampered_signature(runs_root):
    (runs_root / "task-a").mkdir()
    (runs_root / "task-a" / "step_1.png").write_bytes(b"fake-png")
    url = signing.sign_url("task-a", "step_1.png")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    bad_sig = "0" * len(query["sig"])

    with pytest.raises(HTTPException) as exc:
        await media.get_media("task-a", "step_1.png", query["e"], bad_sig)
    assert exc.value.status_code == 403


async def test_media_serves_signed_png(runs_root):
    (runs_root / "task-a").mkdir()
    target = runs_root / "task-a" / "step_1.png"
    target.write_bytes(b"fake-png")
    url = signing.sign_url("task-a", "step_1.png")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))

    resp = await media.get_media("task-a", "step_1.png", query["e"], query["sig"])
    assert resp.path == target


async def test_media_rejects_path_traversal(runs_root):
    """task_id 传 `..` 时必须在验签之前就被拦掉。

    如果只靠签名兜底，那就等于"签名一旦泄露、加上一个穿越路径，
    能读到 runs 之外的任意文件"。防御要叠，不能只押一层。
    """
    with pytest.raises(HTTPException) as exc:
        await media.get_media("..", "step_1.png", "9999999999", "whatever")
    assert exc.value.status_code in (400, 403)


async def test_media_404_when_signed_but_missing(runs_root):
    url = signing.sign_url("task-a", "step_1.png")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    with pytest.raises(HTTPException) as exc:
        await media.get_media("task-a", "step_1.png", query["e"], query["sig"])
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 3. 列表隔离


def _db_reachable() -> bool:
    url = make_url(settings.database_url)
    try:
        with socket.create_connection((url.host or "127.0.0.1", url.port or 5432), timeout=0.5):
            return True
    except OSError:
        return False


db_required = pytest.mark.skipif(
    not _db_reachable(), reason="PostgreSQL 未启动；用 docker 起 wp-postgres 容器后再跑"
)

_PREFIX = "isolation-test-"


@db_required
async def test_task_list_only_returns_own_tasks():
    """隔离的第三个面：列表。

    详情接口挡的是"按 id 精确访问"，列表泄露的是"存在性 + 标题"。
    造两个用户各一个任务，断言 A 的列表里没有 B 的任务。
    """
    async with SessionLocal() as session:
        alice = User(email=f"{_PREFIX}alice@example.com", password_hash="x")
        bob = User(email=f"{_PREFIX}bob@example.com", password_hash="x")
        session.add_all([alice, bob])
        await session.commit()
        await session.refresh(alice)
        await session.refresh(bob)
        alice_id, bob_id = alice.id, bob.id

    await repo.create_task(
        task_id=f"{_PREFIX}a1", goal="A 的任务", start_url="http://127.0.0.1/",
        model="test", user_id=alice_id,
    )
    await repo.create_task(
        task_id=f"{_PREFIX}b1", goal="B 的任务", start_url="http://127.0.0.1/",
        model="test", user_id=bob_id,
    )

    try:
        mine = await repo.fetch_task_list(user_id=alice_id, limit=100)
        goals = [item["goal"] for item in mine["items"]]
        assert "A 的任务" in goals
        assert "B 的任务" not in goals

        # 不加过滤（认证关闭的本地模式）时才应该看到全部
        everyone = await repo.fetch_task_list(user_id=None, limit=100)
        all_goals = [item["goal"] for item in everyone["items"]]
        assert "A 的任务" in all_goals and "B 的任务" in all_goals
    finally:
        # 清干净，不污染真库（task_steps 由外键 CASCADE 处理；这两个任务没有 step）
        from sqlalchemy import delete

        async with SessionLocal() as session:
            await session.execute(delete(Task).where(Task.id.like(f"{_PREFIX}%")))
            await session.execute(delete(User).where(User.email.like(f"{_PREFIX}%")))
            await session.commit()
