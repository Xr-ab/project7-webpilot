"""P5b-② 验收：密码**打码打得干净、还原还得回去**（真实闸门 + 真实批准续跑）。

为什么要有这个脚本（单测已有 3 条）：
单测证明的是"清洗函数对不对"，证明不了三件隔着进程和落盘的事：

1. **明文真的没有落库落盘**：workflow 打码 → meta.json / approvals 表 /
   runs/*/trace.json 三处落点，中间隔着 on_approval 回调、SQL 列、文件写入，
   任何一环漏了单测都不会红——只能拿真实产物来 grep。
2. **批准续跑真的把密码填回去了**：库里的 text 是 `***`，真值靠 `_secret`
   密文还原。如果还原这环坏了（比如填了字面 `***`），后面的登录必然失败——
   这个失败恰恰是"打码正确但还原断了"的唯一可见症状。
3. **登录能走通**：填对真密码 → 点登录 → 跳到 account.html。
   这证明"打码 → 落库 → 批准 → 解密 → 重放"整条链是通的。

跑法（要求：API 在 8000、worker 在跑、PostgreSQL 可用）：
    cd backend && <venv python> ../scripts/check_redact.py

⚠️ 边界要交代清楚（不是打码逻辑偷懒，是它防的范围本来就不含这个）：
goal 文本里写密码 → 模型的 thought 可能复述它 → 非密码步骤的 trace 行不清洗。
打码防的是**动作层产生的明文**（type 动作落库/落盘/回放）；
"goal 里别写密码、用登录态保存"是 P5b credential 机制要消灭的反模式。
所以本脚本的断言瞄准三处硬落点，不赌全轨迹无明文。
"""
from __future__ import annotations

import os
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

# 脚本在 scripts/，但要用 backend 里的 app.* —— 手动把 backend 放进 sys.path
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, text  # noqa: E402

from app.db.models import Approval  # noqa: E402
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

# 测试站写死的账号（与 testsite/login.html 的 ACCOUNTS 一致，页面上不展示密码）。
# 密码必须是真的——批准续跑后要靠"登录成功"证明还原正确
SITE_USER = "zhangsan"
SECRET = "pass1234"

STAMP = time.strftime("%m%d%H%M%S")
PASSWORD = "red-nykwp-416"  # 测试账号密码，与站点密码无关

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
    creds = {"email": email, "password": PASSWORD}
    resp = await client.post("/api/v1/auth/register", json=creds)
    if resp.status_code >= 400:
        resp = await client.post("/api/v1/auth/login", json=creds)
    resp.raise_for_status()
    return resp.json()


async def settle(
    client: httpx.AsyncClient, task_id: str, headers: dict, *, auto_approve: int = 3
) -> dict:
    """轮询到终态。途中遇到 waiting_approval 自动批准（最多 auto_approve 次）。

    为什么要自动批准：密码动作被拦一次是本脚本要验的主流程；
    之后模型如果又做了别的风险动作（比如点"登录"按钮也命中关键词），
    不批的话任务会永远停在 waiting_approval，后面就没法验了。
    """
    deadline = time.time() + 240
    while time.time() < deadline:
        meta = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
        status = meta.get("status")
        if status == "waiting_approval" and auto_approve > 0:
            await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                headers=headers,
                json={"decision": "approve", "note": "check_redact 自动批准"},
            )
            auto_approve -= 1
            await asyncio.sleep(2)
            continue
        if status not in ("queued", "running", "waiting_approval"):
            return meta
        await asyncio.sleep(2)
    return meta


async def db_approval_rows(task_id: str) -> list[dict]:
    """直接从库里读审批行的 action 列（接口层永远不该看到密文以外的东西）。"""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT id, status, action FROM approvals WHERE task_id = :t"),
                {"t": task_id},
            )
        ).all()
        return [{"id": r[0], "status": r[1], "action": r[2]} for r in rows]


async def cleanup(task_id: str, email: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Approval).where(Approval.task_id == task_id))
        await session.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": task_id})
        await session.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
        await session.commit()
    # 磁盘上的 runs 目录也要删，不能只清 DB：meta["goal"] 里写着测试密码，
    # DB 删了目录还在，就留下一个带明文的孤儿目录（上一版漏了这步，
    # 被"落盘无明文"巡检抓出来 5 个）
    shutil.rmtree(files.task_dir(task_id), ignore_errors=True)


async def main() -> int:
    email = f"chk-redact-{STAMP}@webpilot.dev"

    async with httpx.AsyncClient(base_url=BASE, timeout=90, trust_env=False) as client:
        print("\n=== 0. 准备账号 ===")
        u = await register(client, email)
        headers = {"Authorization": f"Bearer {u['token']}"}
        check("测试账号就绪", bool(u.get("token")))

        # ---------------------------------------------------------------- 1
        print("\n=== 1. 提交'往密码框填密码'的任务，等闸门拦下 ===")
        resp = await client.post(
            "/api/v1/tasks",
            headers=headers,
            # 本脚本就是靠"闸门拦下密码动作"做验收的，必须显式 True：
            # common.task_payload 默认 False 会把闸门整个关掉，任务直接跑完，
            # 后面所有断言都会落空
            json=task_payload(
                f"在登录页输入用户名 {SITE_USER}、密码 {SECRET}，"
                "然后点击登录按钮，登录成功后告诉我账户页上有什么。",
                LOGIN_URL,
                require_approval=True,
            ),
        )
        check("提交成功", resp.status_code < 400, f"{resp.status_code} {resp.text[:200]}")
        task_id = resp.json().get("task_id")

        meta = await settle(client, task_id, headers)
        check(
            "任务跑到终态（不是卡死/报错）",
            meta.get("status") not in ("queued", "running", "waiting_approval", "error"),
            str(meta.get("status")),
        )

        # ---------------------------------------------------------------- 2
        print("\n=== 2. 三处硬落点都不许有明文 ===")
        # 落点 1：approvals 表的 action 列（SQL 直查，绕过所有 API 层）
        approval_rows = await db_approval_rows(task_id)
        check("库里至少有一条审批记录", len(approval_rows) >= 1, str(len(approval_rows)))
        bad_rows = [
            r["id"] for r in approval_rows if SECRET in json.dumps(r["action"], ensure_ascii=False)
        ]
        check("approvals 表 action 列无明文", not bad_rows, str(bad_rows))
        password_rows = [
            r for r in approval_rows if (r["action"] or {}).get("action") == "type"
        ]
        check("库里记录的正是那个 type 动作", bool(password_rows), str(approval_rows)[:200])
        boxed = password_rows[0]["action"].get("_secret") if password_rows else None
        check("action 里有 _secret 密文", bool(boxed), str(password_rows[0])[:200] if password_rows else "")
        check(
            "_secret 能解回真密码",
            bool(boxed) and vault.decrypt_text(boxed) == SECRET,
            str(boxed)[:80] if boxed else "无",
        )
        check("库里 text 是 *** 不是明文", password_rows[0]["action"].get("text") == "***" if password_rows else False)

        # 落点 2：meta.json 的 approval 段
        meta_disk = files.load_meta(task_id) or {}
        # 注：meta["goal"] 里**本来就有**明文（用户提交任务时写的），
        # 这不是打码的泄漏——断言只看 approval 段和 trace
        approval_meta = meta_disk.get("approval") or {}
        check(
            "meta.approval 里无明文",
            SECRET not in json.dumps(approval_meta, ensure_ascii=False),
            json.dumps(approval_meta, ensure_ascii=False)[:200],
        )

        # 落点 3：落盘 trace.json 里"暂停那一步"所在的行
        trace = files.load_trace(task_id) or []
        paused_rows = [r for r in trace if r.get("pending_approval")]
        check("trace 里有暂停行", bool(paused_rows), str(len(trace)))
        if paused_rows:
            row = paused_rows[0]
            blob = json.dumps(row, ensure_ascii=False)
            check("暂停行的 action 是 ***", "***" in str(row.get("action")), str(row.get("action")))
            check("暂停行整行无明文", SECRET not in blob, blob[:200])
            payload = (row.get("decision") or {}).get("payload") or {}
            check("暂停行的 decision.payload.text 也打码了", payload.get("text") == "***", str(payload)[:200])

        # ---------------------------------------------------------------- 3
        print("\n=== 3. 批准续跑：真密码真的被填进去了（登录成功是硬证据） ===")
        # 如果还原坏了（把字面 "***" 填进框），登录必然失败，
        # 任务就到不了 account.html——"见过账户页"反过来证明还原正确
        saw_account = any(
            "account.html" in str(r.get("url") or "")
            or "account.html" in str((r.get("decision") or {}).get("snapshot_text") or "")
            for r in trace
        )
        check("续跑后 Agent 见过账户页（登录成功）", saw_account)
        answer = str(meta.get("answer") or "")
        check(
            "结论提到了账户内容",
            bool(answer),
            "answer 为空",
        )
        check("结论本身无明文", SECRET not in answer, answer[:120])

        # 续跑产生的 trace 行（批准执行那一行）也不落明文
        trace_full = files.load_trace(task_id) or []
        type_rows = [
            r
            for r in trace_full
            if str(r.get("action") or "").startswith("type") and SECRET in json.dumps(r, ensure_ascii=False)
        ]
        check("所有 type 行的落盘记录无明文", not type_rows, str(type_rows)[:200])

    print(f"\n=== 结果：{len(PASSED)} 通过 / {len(FAILED)} 失败 ===")
    if FAILED:
        for name in FAILED:
            print(f"  失败：{name}")
        print(f"\n⚠️ 保留了任务 {task_id} 便于排查")
        return 1

    await cleanup(task_id, email)
    print("已清理测试账号与任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
