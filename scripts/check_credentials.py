"""P5b 验收：登录态**存得安全、用得起来、坏了不留坑**（真实登录 + 真实注入）。

为什么要有这个脚本（单元测试已经有 20 条了）：
那 20 条测的是"判定"——这个 id 是不是你的、过期怎么算、密文坏了返回什么。
它们全都把浏览器替换掉了，所以证明不了三件只有端到端才能证明的事：

1. **自动登录真的能登进去**：填账号 → 找密码框前面那个输入框 → 点提交 →
   密码框消失。这一整套依赖真实 DOM，假的 page 上永远是对的。
2. **注入真的改变了页面**：同一张 account.html，匿名进去被弹到 login.html，
   带登录态进去能读到订单表。这是 P5b 的全部价值所在，
   而"storage_state 传进去了"这个断言证明不了它生效。
3. **链路是通的**：API 存 → DB 加密列 → worker 解密 → 浏览器 context。
   中间隔着进程、队列、数据库，任何一环断了单元测试都不会红。

跑法（要求：API 在 8000、worker 在跑、PostgreSQL 可用）：
    cd backend && <venv python> ../scripts/check_credentials.py

会用测试站写死的账号（zhangsan / pass1234）真登录一次，最后清掉测试数据。

⚠️ 这个脚本会在**本进程**里起 Chromium 做两次页面访问（第 5 段）。
不让 worker 去跑，是为了拿确定性：LLM 说不说得出订单号，不该成为
"注入有没有生效"的判据。第 6 段才走真实任务，验的是链路串通。
"""
from __future__ import annotations

import os
import asyncio
import shutil
import sys
import time
from pathlib import Path

import httpx
from urllib.parse import urlparse

# 脚本在 scripts/，但要用 backend 里的 app.* —— 手动把 backend 放进 sys.path
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, text  # noqa: E402

from app.browser.session import BrowserSession  # noqa: E402
from app.db.models import Approval, Credential  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.security import vault  # noqa: E402
from app.storage import files  # noqa: E402
from common import task_payload  # noqa: E402

# BASE 可用环境变量覆盖：容器里跑时 API 调用和 Chromium 的 start_url
# 都必须走服务名 http://api:8000（worker 容器的 127.0.0.1 不是 api，
# 这是容器化最经典的一跤，2026-10-28 实测踩过）
BASE = os.environ.get("WEBPILOT_BASE", "http://127.0.0.1:8000")
LOGIN_URL = f"{BASE}/testsite/login.html"
ACCOUNT_URL = f"{BASE}/testsite/account.html"

# 测试站写死的账号（见 testsite/login.html 里的 ACCOUNTS）
SITE_USER = "zhangsan"
SITE_PASS = "pass1234"
SITE_USER_B = "lisi"
SITE_PASS_B = "pass5678"

# 只有登录后才看得到的订单号——"注入生效"的硬证据
ORDER_NO = "WP-1001"

STAMP = time.strftime("%m%d%H%M%S")
PASSWORD = "cred-lfcyf-974"  # 我们自己的测试账号密码，和被保存的站点密码无关

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  \u2713 {name}")
    else:
        FAILED.append(name)
        print(f"  \u2717 {name}   {detail}")


async def register(client: httpx.AsyncClient, email: str) -> dict:
    """注册并返回 {user_id, email, token}。邮箱已存在时改走登录。"""
    creds = {"email": email, "password": PASSWORD}
    resp = await client.post("/api/v1/auth/register", json=creds)
    if resp.status_code >= 400:
        resp = await client.post("/api/v1/auth/login", json=creds)
    resp.raise_for_status()
    return resp.json()


async def wait_settled(client: httpx.AsyncClient, task_id: str, headers: dict) -> dict:
    """轮询到任务离开 queued/running，返回最后一次拿到的 meta。"""
    deadline = time.time() + 180
    meta: dict = {}
    while time.time() < deadline:
        meta = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
        if meta.get("status") not in ("queued", "running"):
            return meta
        await asyncio.sleep(2)
    return meta


async def db_credential_row(credential_id: int):
    """直接从库里读一行（要看 ciphertext，接口永远不给）。"""
    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT id, user_id, site, label, ciphertext, expires_at "
                    "FROM credentials WHERE id = :i"
                ),
                {"i": credential_id},
            )
        ).first()


async def db_credential_count() -> int:
    async with SessionLocal() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM credentials"))).scalar() or 0
        )


async def visit(url: str, storage_state: dict | None = None) -> tuple[str, str]:
    """打开一个页面，返回 (最终 url, 正文文本)。

    为什么要等一下再读 url：account.html 的跳转是 JS 里 `location.replace` 触发的，
    它跑在 domcontentloaded 之后。不等就会读到"还没跳"的中间态，
    于是匿名访问看起来也停在 account.html —— 断言就白写了。
    """
    async with BrowserSession(headless=True, storage_state=storage_state) as session:
        await session.goto(url)
        await session.page.wait_for_timeout(700)
        return session.page.url, await session.page.inner_text("body")


async def cleanup(task_ids: list[str], emails: list[str]) -> None:
    """按 id/邮箱精确清理，不碰真库里的其他数据。"""
    async with SessionLocal() as session:
        for tid in task_ids:
            await session.execute(delete(Approval).where(Approval.task_id == tid))
            await session.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": tid})
        for email in emails:
            # credentials 有 ON DELETE CASCADE，删用户会连带删掉登录态
            await session.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
        await session.commit()
    # runs 目录跟着删：DB 清了磁盘不删 = 留孤儿目录（goal 可能带敏感词），
    # "已清理测试账号与任务"的承诺要包含磁盘
    for tid in task_ids:
        shutil.rmtree(files.task_dir(tid), ignore_errors=True)


async def main() -> int:
    email_a = f"chk-cred-a-{STAMP}@webpilot.dev"
    email_b = f"chk-cred-b-{STAMP}@webpilot.dev"
    created_tasks: list[str] = []

    # trust_env=False：本机开着代理时 httpx 会把 127.0.0.1 也塞给代理，直接 502
    async with httpx.AsyncClient(base_url=BASE, timeout=90, trust_env=False) as client:
        print("\n=== 0. 准备两个账号 ===")
        a = await register(client, email_a)
        b = await register(client, email_b)
        ha = {"Authorization": f"Bearer {a['token']}"}
        hb = {"Authorization": f"Bearer {b['token']}"}
        check("两个测试账号就绪", bool(a.get("token") and b.get("token")))

        before = await db_credential_count()

        # ---------------------------------------------------------------- 1
        print("\n=== 1. 保存登录态（真的登录一次） ===")
        resp = await client.post(
            "/api/v1/credentials",
            headers=ha,
            json={"login_url": LOGIN_URL, "username": SITE_USER, "password": SITE_PASS},
        )
        check("保存成功（2xx）", resp.status_code < 400, f"{resp.status_code} {resp.text[:200]}")
        saved = resp.json() if resp.status_code < 400 else {}
        cid = saved.get("id")
        check("返回了 id", isinstance(cid, int), str(saved))
        check("拿到了 cookie", int(saved.get("cookie_count") or 0) > 0, str(saved))
        check("站点只到 host（不含端口）", saved.get("site") == urlparse(BASE).hostname, str(saved.get("site")))
        check("label 默认用账号名", saved.get("label") == SITE_USER, str(saved.get("label")))
        # 测试站的 cookie 带 max-age=604800，所以应该能算出过期时间
        check("算出了过期时间", bool(saved.get("expires_at")), str(saved.get("expires_at")))

        # 响应体里**不能**出现会话内容：这是"永远不回密文"的那条纪律
        body = resp.text
        check(
            "响应体不含 cookie 值/密文",
            "wp_session" not in body and "ciphertext" not in body,
            body[:300],
        )

        # ---------------------------------------------------------------- 2
        print("\n=== 2. 库里的东西（这条是安全底线） ===")
        row = await db_credential_row(cid) if cid else None
        check("库里有一行", row is not None)
        if row is not None:
            ciphertext = row[4] or ""
            check("ciphertext 非空", len(ciphertext) > 50, f"len={len(ciphertext)}")
            # 最关键的一条：明文密码、账号名、cookie 名都不能出现在密文里
            for secret in (SITE_PASS, SITE_USER, "wp_session"):
                check(
                    f"密文不含明文片段 {secret!r}",
                    secret not in ciphertext,
                    ciphertext[:80],
                )
            state = vault.decrypt(ciphertext)
            check("能解密回 storage_state", isinstance(state, dict) and "cookies" in state)
            check(
                "解密后确实有 wp_session",
                any(c.get("name") == "wp_session" for c in state.get("cookies") or []),
                str([c.get("name") for c in state.get("cookies") or []]),
            )
            check("归属用户是 A", int(row[1]) == int(a["user_id"]), f"{row[1]} vs {a['user_id']}")

        # ---------------------------------------------------------------- 3
        print("\n=== 3. 密码错了要如实报错，且不许落库 ===")
        mid = await db_credential_count()
        resp = await client.post(
            "/api/v1/credentials",
            headers=ha,
            json={"login_url": LOGIN_URL, "username": SITE_USER, "password": "wrong-pass-xyz"},
        )
        check("错密码 → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:200]}")
        check("错误信息指向账密问题", "账号或密码" in resp.text, resp.text[:200])
        # 报错信息里绝不能回显用户输入的密码
        check("错误信息不回显密码", "wrong-pass-xyz" not in resp.text, resp.text[:300])
        check("库里没有多出一行", await db_credential_count() == mid)

        # ---------------------------------------------------------------- 4
        print("\n=== 4. 列表与归属隔离 ===")
        resp = await client.get("/api/v1/credentials", headers=ha)
        data = resp.json()
        check("A 能列出自己的", data.get("available") is True and len(data.get("items") or []) >= 1)
        item = (data.get("items") or [{}])[0]
        check("列表项不含密文", "ciphertext" not in item, str(list(item)))
        check("列表项带 id/站点/标签", {"id", "site", "label"} <= set(item), str(list(item)))

        # B 是另一个账号：既看不到 A 的列表，也不能删、不能用
        resp = await client.get("/api/v1/credentials", headers=hb)
        ids_b = [i["id"] for i in resp.json().get("items") or []]
        check("B 的列表里没有 A 的登录态", cid not in ids_b, str(ids_b))

        resp = await client.delete(f"/api/v1/credentials/{cid}", headers=hb)
        check("B 删 A 的 → 404（不是 403）", resp.status_code == 404, str(resp.status_code))

        resp = await client.post(
            "/api/v1/tasks",
            headers=hb,
            json=task_payload("读一下订单", ACCOUNT_URL, credential_id=cid),
        )
        check("B 拿 A 的登录态提任务 → 404", resp.status_code == 404, str(resp.status_code))

        # ---------------------------------------------------------------- 5
        print("\n=== 5. 注入到底有没有用（同一页面，匿名 vs 登录态） ===")
        anon_url, anon_text = await visit(ACCOUNT_URL)
        check(
            "匿名访问被弹到登录页",
            "login.html" in anon_url,
            f"url={anon_url}",
        )
        check(
            "匿名访问看不到订单",
            ORDER_NO not in anon_text,
            anon_text[:120].replace("\n", " "),
        )

        authed_url, authed_text = await visit(ACCOUNT_URL, storage_state=state)
        check(
            "带登录态访问停在账户页",
            "account.html" in authed_url,
            f"url={authed_url}",
        )
        check(
            "带登录态能看到订单表",
            ORDER_NO in authed_text,
            authed_text[:200].replace("\n", " "),
        )
        check("页面识别出的用户名就是 zhangsan", SITE_USER in authed_text, authed_text[:200])

        # ---------------------------------------------------------------- 6
        print("\n=== 6. 真实任务链路（提交 → worker 解密 → 注入 → 落 meta） ===")
        resp = await client.post(
            "/api/v1/tasks",
            headers=ha,
            json=task_payload(
                "打开我的账户页面，读出第一个订单的订单号并告诉我",
                ACCOUNT_URL,
                credential_id=cid,
            ),  # require_approval 默认 False：这一步只验注入链路，别被审批闸门绊住
        )
        check("提交成功", resp.status_code < 400, f"{resp.status_code} {resp.text[:200]}")
        task_id = resp.json().get("task_id")
        created_tasks.append(task_id)

        meta = await wait_settled(client, task_id, ha)
        cred_meta = meta.get("credential") or {}
        check("任务跑到了终态", meta.get("status") not in ("queued", "running"), str(meta.get("status")))
        check(
            "meta 里记下了'本次带登录态'",
            cred_meta.get("ok") is True,
            str(cred_meta),
        )
        check("meta 里的 label 是 zhangsan", cred_meta.get("label") == SITE_USER, str(cred_meta))
        check(
            "meta 里的 credential_id 对得上",
            int(cred_meta.get("credential_id") or -1) == int(cid),
            str(cred_meta),
        )

        # 任务里**不该**出现过登录页：开局就是登录态，模型没理由去登录。
        #
        # 为什么不用"有没有往密码框里 type"当判据：trace 每行的 action 是个字符串
        # （形如 `finish 'WP-1001'`、`type e3 '...'`），target 是当轮编号，
        # 从这串文字里看不出那个框是不是密码框——硬解析只会写出一个脆断言。
        # 换个更本质的判据：**它压根没去过 login.html**。没到登录页，就不可能填密码。
        trace = (await client.get(f"/api/v1/tasks/{task_id}/trace", headers=ha)).json()
        rows = trace.get("trace") or []
        visited_login = [
            r
            for r in rows
            if "login.html" in str(r.get("url") or "")
            or "login.html" in str((r.get("decision") or {}).get("snapshot_text") or "")
        ]
        check("全程没去过登录页", not visited_login, str(len(visited_login)))
        check(
            "开头就在账户页（没先被弹走）",
            bool(rows) and "account.html" in str(rows[0].get("url") or ""),
            str(rows[0].get("url") if rows else None),
        )
        # P5b 的最终目的：Agent 有身份之后能**真的把登录后才看得到的信息读出来**。
        # 这一条依赖模型复述，所以放在最后——它失败时前面的确定性断言已经定位了问题。
        check(
            "任务结论里读出了订单号",
            ORDER_NO in str(meta.get("answer") or ""),
            str(meta.get("answer"))[:120],
        )

        # touch_credential 生效：被用过的登录态要记上时间
        resp = await client.get("/api/v1/credentials", headers=ha)
        used = [i for i in resp.json().get("items") or [] if i["id"] == cid]
        check("last_used_at 被更新了", bool(used and used[0].get("last_used_at")), str(used)[:200])

        # ---------------------------------------------------------------- 7
        print("\n=== 7. 密文坏了：任务照跑，但不假装自己是登录的 ===")
        async with SessionLocal() as session:
            await session.execute(
                text("UPDATE credentials SET ciphertext = 'not-a-valid-token' WHERE id = :i"),
                {"i": cid},
            )
            await session.commit()

        resp = await client.post(
            "/api/v1/tasks",
            headers=ha,
            json=task_payload("打开我的账户页面，看看有什么", ACCOUNT_URL, credential_id=cid),
        )
        check("坏密文也允许提交（不阻断）", resp.status_code < 400, f"{resp.status_code}")
        bad_id = resp.json().get("task_id")
        created_tasks.append(bad_id)

        meta = await wait_settled(client, bad_id, ha)
        cred_meta = meta.get("credential") or {}
        check(
            "meta 里如实记了 ok=False",
            cred_meta.get("ok") is False,
            str(cred_meta),
        )
        check(
            "提示里让人重新保存",
            "重新保存" in (cred_meta.get("note") or ""),
            str(cred_meta.get("note")),
        )
        # 关键：任务本身还是跑完了，没有因为凭证坏掉而报错
        check(
            "任务仍然跑到了终态（没被凭证问题拖死）",
            meta.get("status") not in ("queued", "running", "error"),
            str(meta.get("status")),
        )

        # ---------------------------------------------------------------- 8
        print("\n=== 8. 删除 ===")
        resp = await client.delete(f"/api/v1/credentials/{cid}", headers=ha)
        check("本人删除成功", resp.status_code < 400, str(resp.status_code))
        check("再删一次 → 404", (await client.delete(f"/api/v1/credentials/{cid}", headers=ha)).status_code == 404)
        resp = await client.get("/api/v1/credentials", headers=ha)
        check(
            "列表里没了",
            all(i["id"] != cid for i in resp.json().get("items") or []),
        )
        check("库里也没有了", await db_credential_row(cid) is None)

        check(
            "没留下多余的登录态行",
            await db_credential_count() == before,
            f"before={before} now={await db_credential_count()}",
        )

    print(f"\n=== 结果：{len(PASSED)} 通过 / {len(FAILED)} 失败 ===")
    if FAILED:
        for name in FAILED:
            print(f"  失败：{name}")
        print(f"\n⚠️ 保留了任务 {created_tasks} 便于排查")
        return 1

    await cleanup(created_tasks, [email_a, email_b])
    print("已清理测试账号与任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
