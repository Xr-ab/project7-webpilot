"""登录态接口（P5b）的单元测试。

分工说明：**这里只测"判定"，不测"登录"**。
- 判定（这个登录态能不能用、过期怎么算）：纯逻辑，能在这里一条条钉死
- 登录（真的开浏览器填密码点按钮）：需要真浏览器 + 真页面，
  放在 `scripts/check_credentials.py` 里跑端到端，用测试站的 login.html

为什么这样切：登录那一半慢（起 Chromium 2~4 秒）、脆（依赖页面结构），
放进 pytest 会让整个套件从 40 秒变成几分钟，于是没人愿意跑。
而它真正容易出错的地方（账密不对、点不到按钮）恰好是**端到端才看得出来**的。
两边各测各的长处。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import credentials as cred_api
from app.api import tasks as tasks_api
from app.browser.login import expires_at_from_state
from app.runner import task_runner
from app.security import vault

ME = SimpleNamespace(id=1)
OTHER_ROW = {"id": 7, "user_id": 2, "site": "127.0.0.1", "label": "别人的"}
MY_ROW = {"id": 7, "user_id": 1, "site": "127.0.0.1", "label": "zhangsan"}

START = "http://127.0.0.1:8000/testsite/index.html"


# ---------------------------------------------------------------- 站点标识

def test_site_uses_host_not_netloc():
    """站点标识只到 host，不带端口。

    为什么这条值得单独一个测试：如果用 netloc（host:port），
    经 Vite 代理（5173）保存的登录态在直连 8000 时就会"明明存了却说没有"——
    而这类问题排查起来极费时间，因为两边看起来是同一个站点。
    """
    assert cred_api._site_of("http://127.0.0.1:8000/testsite/login.html") == "127.0.0.1"
    assert cred_api._site_of("http://127.0.0.1:5173/testsite/login.html") == "127.0.0.1"
    # 大小写不敏感：Host 头是大小写不敏感的，数据库里存小写才能对上
    assert cred_api._site_of("http://LocalHost:8000/x") == "localhost"


def test_site_of_garbage_returns_empty():
    """解析不出站点时返回空串，由调用方报 400（而不是在这里抛）。"""
    assert cred_api._site_of("not-a-url") == ""


# ---------------------------------------------------------------- 过期计算

def test_expires_takes_the_latest_cookie():
    """取最晚过期的那个 cookie，不是最早。

    理由：真实站点里除了会话 cookie，还会有一堆统计/偏好 cookie。
    取最早会把"长期有效的会话 + 一个 10 分钟后过期的统计 cookie"
    判成"马上失效"，让用户白重新登录一次。
    """
    now = datetime.now(tz=timezone.utc).timestamp()
    state = {
        "cookies": [
            {"name": "session", "expires": now + 86400},
            {"name": "_ga", "expires": now + 60},
        ]
    }
    got = expires_at_from_state(state)
    assert got is not None
    assert abs(got.timestamp() - (now + 86400)) < 2


def test_expires_returns_none_for_session_cookies():
    """全是会话 cookie（expires = -1）→ None。

    返回 None 而不是 1970：前端据此显示"会话型（关浏览器失效）"，
    比编一个假时间诚实。编时间的后果是用户看到"1970 年过期"以为系统坏了。
    """
    state = {"cookies": [{"name": "a", "expires": -1}, {"name": "b", "expires": 0}]}
    assert expires_at_from_state(state) is None
    assert expires_at_from_state({"cookies": []}) is None


# ---------------------------------------------------------------- 提交时校验

async def test_credential_check_rejects_without_user():
    """认证关闭模式下没有归属人 → 400。

    为什么不允许"无主登录态"：登录态是凭证，没有主人就没法做归属隔离，
    于是所有人都能用同一份——那比不存更糟（一个泄露影响全站）。
    """
    with pytest.raises(HTTPException) as exc:
        await tasks_api._assert_credential_usable(7, None, START)
    assert exc.value.status_code == 400


async def test_credential_check_hides_other_peoples(monkeypatch):
    """别人的登录态 → 404（不是 403），和删除接口同一口径。

    repo 层已经用 user_id 过滤，所以"别人的"在这里表现为"查不到"。
    这条测试锁的是**上层不许自作聪明地改成 403** —— 403 等于承认 id 存在，
    而 id 自增，攻击者能据此数出全站存了多少份登录态。
    """
    async def fake_fetch(credential_id, user_id=None):
        return OTHER_ROW if user_id == 2 else None

    monkeypatch.setattr(tasks_api.repo, "fetch_credential", fake_fetch)
    with pytest.raises(HTTPException) as exc:
        await tasks_api._assert_credential_usable(7, ME, START)
    assert exc.value.status_code == 404


async def test_credential_check_passes_for_own(monkeypatch):
    """自己的登录态 + 站点一致 → 放行。"""
    async def fake_fetch(credential_id, user_id=None):
        return MY_ROW

    monkeypatch.setattr(tasks_api.repo, "fetch_credential", fake_fetch)
    await tasks_api._assert_credential_usable(7, ME, START)  # 不抛即为通过


async def test_credential_check_does_not_block_on_site_mismatch(monkeypatch):
    """站点不一致**只警告不拦**。

    这个看起来像"测试写得松"，其实是在锁一个决定：SSO 是真实存在的
    （sso.a.com 登录 → 跳到 app.a.com）。硬拦会误伤它；
    而 cookie 的域由浏览器自己判，注入错了只是没生效，不会串号。
    所以这里断言的是"能过"——如果有人改成 raise，这条会红，
    逼他回来重新论证。
    """
    async def fake_fetch(credential_id, user_id=None):
        return {**MY_ROW, "site": "example.com"}

    monkeypatch.setattr(tasks_api.repo, "fetch_credential", fake_fetch)
    await tasks_api._assert_credential_usable(7, ME, START)


# ---------------------------------------------------------------- 执行时注入

async def test_resolve_returns_none_when_no_credential():
    """任务没指定 credential_id → 匿名跑，report 为空（前端什么都不显示）。"""
    state, report = await task_runner._resolve_storage_state({}, user_id=1)
    assert state is None
    assert report == {}


async def test_resolve_reports_deleted_credential(monkeypatch):
    """保存过但已经被删 → 照跑，但记一句说明。

    **不抛异常**是这个测试的重点。用户提交的是"读一下订单列表"，
    因为一份几个月前的登录态被删了就让整个任务失败，是本末倒置。
    """
    async def fake_fetch(credential_id, user_id=None):
        return None

    monkeypatch.setattr(task_runner.repo, "fetch_credential", fake_fetch)
    state, report = await task_runner._resolve_storage_state(
        {"credential_id": 7}, user_id=1
    )
    assert state is None
    assert report["ok"] is False
    assert "按未登录执行" in report["note"]


async def test_resolve_reports_decrypt_failure(monkeypatch):
    """密文坏了（密钥轮换 / 数据损坏）→ 照跑 + 明确让人重新保存。

    为什么要区分"被删了"和"解不开"：前者不用用户做事，后者要他去重新保存。
    两种情况的提示文字如果一样，用户就不知道该不该动手。
    """
    async def fake_fetch(credential_id, user_id=None):
        return {**MY_ROW, "ciphertext": "garbage"}

    monkeypatch.setattr(task_runner.repo, "fetch_credential", fake_fetch)
    state, report = await task_runner._resolve_storage_state(
        {"credential_id": 7}, user_id=1
    )
    assert state is None
    assert report["ok"] is False
    assert "重新保存" in report["note"]


async def test_resolve_injects_state_and_touches(monkeypatch):
    """正常路径：解出 storage_state、返回 ok，并且把"最近使用"记上。"""
    payload = {"cookies": [{"name": "wp_session", "value": "abc"}], "origins": []}
    touched: list[int] = []

    async def fake_fetch(credential_id, user_id=None):
        return {**MY_ROW, "ciphertext": vault.encrypt(payload)}

    async def fake_touch(credential_id):
        touched.append(credential_id)

    monkeypatch.setattr(task_runner.repo, "fetch_credential", fake_fetch)
    monkeypatch.setattr(task_runner.repo, "touch_credential", fake_touch)

    state, report = await task_runner._resolve_storage_state(
        {"credential_id": 7}, user_id=1
    )
    assert state == payload
    assert report["ok"] is True
    assert report["label"] == "zhangsan"
    assert touched == [7]


async def test_resolve_warns_but_still_uses_expired(monkeypatch):
    """过期了也照样注入，只加一句提示。

    为什么不拦：cookie 的 expires 是个"声明"，很多站点实际容忍更久；
    反过来也有"没过期但服务端早踢了"的情况。它只是提示，不是判据。

    ⚠️ 这里的 fake row 用的是 `{"expired": True}` 而不是一个过去的 datetime：
    `_resolve_storage_state` 的入参契约是 **repo._credential_dict 加工后的形状**
    （时间已经格式化成字符串、过期与否已经算成布尔），不是 DB 原始行。
    第一版实现里我拿 `row["expires_at"].timestamp()` 去比，直接 AttributeError——
    因为那个字段到这儿已经是字符串了。这条测试用 expired 布尔，
    就是在把"过期判定只有一处实现"这件事固定下来。
    """
    payload = {"cookies": [], "origins": []}

    async def fake_fetch(credential_id, user_id=None):
        return {**MY_ROW, "ciphertext": vault.encrypt(payload), "expired": True}

    async def fake_touch(credential_id):
        return None

    monkeypatch.setattr(task_runner.repo, "fetch_credential", fake_fetch)
    monkeypatch.setattr(task_runner.repo, "touch_credential", fake_touch)

    state, report = await task_runner._resolve_storage_state(
        {"credential_id": 7}, user_id=1
    )
    assert state == payload, "过期不该导致不注入"
    assert report["ok"] is True
    assert "过期" in report["note"]
