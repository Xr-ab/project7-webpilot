"""P5a 验收：审批**落库、可查、隔离、批准续跑、拒绝重新决策**（真实双账号互打）。

为什么要有这个脚本（单元测试已经 49 条了）：
单元测试全是"函数级"的——`repo.create_approval` 写进去、`fetch_pending` 读出来、
`_resume_start` 用一个假 session 验调度。它们证明不了**链路**：
审批是 worker 进程写的、接口是 API 进程读的、续跑是又一个 worker job 跑的，
中间隔着 redis 队列、DB、meta.json 三个介质。
这个脚本把整条链路真跑一遍，出错时报的是"哪一环断了"，而不是"哪一行断言红了"。

跑法（要求：API 在 8000、worker 在跑、PostgreSQL 可用）：
    cd backend && <venv python> ../scripts/check_approval.py

它会真的走两条完整链路：
    提交 → 暂停 → **批准** → 续跑 → 终态
    提交 → 暂停 → **拒绝** → 带着理由重新决策 → 终态
然后把测试账号和任务都清掉。
"""
from __future__ import annotations

import os
import asyncio
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
from app.storage import files  # noqa: E402

from common import task_payload  # noqa: E402  脚本直跑时 sys.path[0] 就是 scripts/

# BASE 可用环境变量覆盖：容器里跑时 API 调用和 Chromium 的 start_url
# 都必须走服务名 http://api:8000（worker 容器的 127.0.0.1 不是 api，
# 这是容器化最经典的一跤，2026-10-28 实测踩过）
BASE = os.environ.get("WEBPILOT_BASE", "http://127.0.0.1:8000")
# 复用已验证能触发闸门的 goal（之前实测这条路会走到「提交」按钮）
GOAL = "在资料填写页填写姓名张三、邮箱 zhangsan@example.com、城市福州，然后点击提交按钮"
START_URL = f"{BASE}/testsite/form.html"
PASSWORD = "approval-check-123"   # 一次性测试账号，register 时现造
STAMP = time.strftime("%m%d%H%M%S")

# 一条任务最多批几次。给 4 次余量：正常 1~2 次就够了，
# 超过说明"批准后模型还在反复触发同一类动作"——那要报出来看，不能无限批下去。
MAX_APPROVALS = 4

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
    """轮询到任务离开 queued/running，返回最后一次拿到的 meta。

    超时 180 秒：一个 5 步任务实测 20~40 秒；给足余量是因为免费档 LLM 偶尔排队。
    续跑之后也用它——"停在 waiting_approval"和"跑到 success"都算 settled。
    """
    deadline = time.time() + 180
    meta: dict = {}
    while time.time() < deadline:
        meta = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
        if meta.get("status") not in ("queued", "running"):
            return meta
        await asyncio.sleep(2)
    return meta


async def db_pending_rows(task_id: str) -> list[tuple]:
    """该任务的全部审批项：id / step_no / status / decided_by / reason / note。

    note 排在最后而不是替换 reason：前面的下标（r[2]=status、r[3]=decided_by）
    已经被现有断言用着，往末尾追加才不会把老断言悄悄改义。
    reason 是"为什么停下来"（闸门写的），note 是"人说了什么"（审批人写的）——
    拒绝时后者会回喂给模型，所以它是 P5a-4 必须验的字段。
    """
    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT id, step_no, status, decided_by, reason, note "
                    "FROM approvals WHERE task_id = :t ORDER BY id"
                ),
                {"t": task_id},
            )
        ).all()


async def db_step_rows(task_id: str) -> list[tuple]:
    """每步在 task_steps 里有几条。用来验"续跑没有把已有轨迹重新插一遍"。"""
    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT step_no, count(*) FROM task_steps "
                    "WHERE task_id = :t GROUP BY step_no ORDER BY step_no"
                ),
                {"t": task_id},
            )
        ).all()


async def cleanup(task_ids: list[str], emails: list[str]) -> None:
    """按 id/邮箱精确清理，不碰真库里的其他数据。"""
    async with SessionLocal() as session:
        for tid in task_ids:
            await session.execute(delete(Approval).where(Approval.task_id == tid))
            await session.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": tid})
        for email in emails:
            await session.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
        await session.commit()


async def main() -> int:
    email_a = f"chk-ap-a-{STAMP}@webpilot.dev"
    email_b = f"chk-ap-b-{STAMP}@webpilot.dev"
    created_tasks: list[str] = []

    # trust_env=False：本机开着代理时 httpx 会把 127.0.0.1 也塞给代理，直接 502
    async with httpx.AsyncClient(base_url=BASE, timeout=60, trust_env=False) as client:
        print("\n=== 0. 准备两个账号 ===")
        a = await register(client, email_a)
        b = await register(client, email_b)
        ha = {"Authorization": f"Bearer {a['token']}"}
        hb = {"Authorization": f"Bearer {b['token']}"}
        check("注册 A/B 两个账号", a["user_id"] != b["user_id"])

        print("\n=== 1. A 提交一个会点「提交」的任务 ===")
        resp = await client.post(
            "/api/v1/tasks",
            # 本脚本就是要测审批，显式 True（common.task_payload 默认 False 是给批处理的）
            json=task_payload(GOAL, START_URL, require_approval=True),
            headers=ha,
        )
        check("提交成功（200）", resp.status_code == 200, resp.text)
        task_id = resp.json()["task_id"]
        created_tasks.append(task_id)
        print(f"    task_id = {task_id}")

        print("\n=== 2. 等它停在 waiting_approval ===")
        meta = await wait_settled(client, task_id, ha)
        check("状态 = waiting_approval", meta.get("status") == "waiting_approval", str(meta.get("status")))
        check("没有写 finished_at（任务没结束）", meta.get("finished_at") is None)
        check("没有写 elapsed_s", meta.get("elapsed_s") is None)
        ap = meta.get("approval") or {}
        check("meta 带 pending_approval 标记", bool(ap))
        check("暂停理由非空", bool(ap.get("reason")), str(ap.get("reason")))
        check("待审批动作是 click", (ap.get("action") or {}).get("action") == "click",
              str(ap.get("action")))
        check("恢复上下文里有 goal", (ap.get("context") or {}).get("goal") == GOAL)

        print("\n=== 3. 审批落库（approvals 表） ===")
        rows = await db_pending_rows(task_id)
        check("表里有且仅有 1 条 pending", len(rows) == 1 and rows[0][2] == "pending", str(rows))
        db_id = rows[0][0] if rows else None
        check("decided_by 为空（还没人批）", rows and rows[0][3] is None)
        check("meta 里的 id 与表里的 id 一致", ap.get("id") == db_id,
              f"meta={ap.get('id')} db={db_id}")
        check("step_no 一致", ap.get("step_no") == (rows[0][1] if rows else -1))

        print("\n=== 4. GET /approval（A 自己查） ===")
        resp = await client.get(f"/api/v1/tasks/{task_id}/approval", headers=ha)
        check("A 查自己的审批 → 200", resp.status_code == 200, resp.text)
        body = resp.json()
        pending = body.get("pending") or {}
        check("pending 非空", bool(pending))
        check("pending.id 来自表（不是 None）", pending.get("id") == db_id, str(pending.get("id")))
        check("pending.status = pending", pending.get("status") == "pending")
        check("waiting_s 是数字且非负",
              isinstance(pending.get("waiting_s"), int) and pending["waiting_s"] >= 0,
              str(pending.get("waiting_s")))
        check("响应里带任务状态", body.get("status") == "waiting_approval")

        print("\n=== 5. 隔离：B 碰 A 的东西 ===")
        resp = await client.get(f"/api/v1/tasks/{task_id}", headers=hb)
        check("B 查 A 的任务 → 404", resp.status_code == 404, str(resp.status_code))
        resp = await client.get(f"/api/v1/tasks/{task_id}/approval", headers=hb)
        check("B 查 A 的审批 → 404", resp.status_code == 404, str(resp.status_code))
        # 计划里的验收线原文："B 的 token 批 A 的审批 → 404"。
        # 这条必须验：光验"读"不够，写通道的权限错了后果更重（等于别人能替你放行高风险动作）
        resp = await client.post(
            f"/api/v1/tasks/{task_id}/approval", json={"decision": "approve"}, headers=hb
        )
        check("B 批 A 的审批 → 404", resp.status_code == 404, str(resp.status_code))
        resp = await client.get("/api/v1/tasks/20260101-000000-nosuch/approval", headers=ha)
        check("不存在的任务 → 404（不是 200 空对象）", resp.status_code == 404,
              str(resp.status_code))

        print("\n=== 6. 批准 → 续跑（可能要批好几次） ===")
        # 为什么是个循环：一个任务可以触发**多次**审批（第 4 步提交、第 7 步又触发），
        # 这不是异常而是设计如此。写死"批一次就该结束"的脚本第一次跑就会红，
        # 而且红得像是 bug —— 实际是脚本的假设错了。
        rounds = 0
        while rounds < MAX_APPROVALS:
            resp = await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                json={"decision": "approve", "note": "确认（验收脚本）"},
                headers=ha,
            )
            check(f"第 {rounds + 1} 次批准返回 200", resp.status_code == 200, resp.text)
            if resp.status_code != 200:
                break
            body = resp.json()
            check(f"第 {rounds + 1} 次：任务回到 queued",
                  body.get("task_status") == "queued", str(body))
            rounds += 1
            meta = await wait_settled(client, task_id, ha)
            if meta.get("status") != "waiting_approval":
                break

        rows = await db_pending_rows(task_id)
        check("所有审批项都变成 approved",
              bool(rows) and all(r[2] == "approved" for r in rows), str(rows))
        check("每一条都记下了审批人",
              all(r[3] == a["user_id"] for r in rows), str(rows))
        print(f"    一共批了 {rounds} 次")

        print("\n=== 7. 等续跑跑完，验 trace 没重复 ===")
        check("最终状态 = success", meta.get("status") == "success", str(meta.get("status")))
        check("写了 finished_at", bool(meta.get("finished_at")))
        check("耗时包含暂停前那几段（不是只剩最后一趟那几秒）",
              (meta.get("elapsed_s") or 0) > 0, str(meta.get("elapsed_s")))

        step_rows = await db_step_rows(task_id)
        by_step: dict[int, int] = {}
        for step_no, count in step_rows:
            by_step[int(step_no)] = int(count)
        # 前 3 步各只有 1 条：续跑没有把已有轨迹重新插一遍。
        # 这一条是整个验收里最重要的断言——插重了不会报错，只会静默翻倍，
        # 然后"数据库比 trace.json 多一倍的行"，这种不一致极难查。
        check("前 3 步没有重复", all(by_step.get(s) == 1 for s in (1, 2, 3)), str(by_step))
        # 第 4 步 2 条：一条"被暂停"、一条"批准后真的执行了"，靠 attempt 区分
        check("第 4 步有 2 条（暂停 + 批准后执行）", by_step.get(4) == 2, str(by_step))
        check("总步数比暂停时多", sum(by_step.values()) > 4, str(by_step))

        # 审批标记只写在 trace.json 里（task_steps 没有这一列），所以读文件。
        # 这不是偷懒：现在除了回放/审计没人读它，等前端要用"已批准执行"的徽标时
        # 再补一列 + 迁移，比现在为一个没人读的布尔值加迁移划算。
        disk_trace = files.load_trace(task_id)
        approved_rows = [r for r in disk_trace if r.get("approved")]
        check("有带审批标记的执行记录", len(approved_rows) >= 1, str(len(approved_rows)))
        # 关键：批准的 click 必须**真的成功**（ok=True）。
        # P5a-3 第一版就是在这里翻的车：元素编号是当轮快照的临时属性，
        # 浏览器重开后点不到，动作"执行了"但 ok=False、页面毫无变化。
        check("批准的动作真的执行成功（ok=True）",
              all(r.get("ok") for r in approved_rows),
              str([(r["step"], r["ok"], (r.get("error") or "")[:40]) for r in approved_rows]))

        # 方案 D 的验收线：不光"点了没报错"，还要"提交的是填好的数据"。
        # 空表单提交时的页面回显是「提交成功：姓名=，邮箱=，城市=未填」，
        # 所以这里直接盯住"姓名=张三"——**动作成功 ≠ 做对了**。
        restored_ok = False
        for i, row in enumerate(disk_trace):
            if not (row.get("approved") and row.get("ok")) or i + 1 >= len(disk_trace):
                continue
            notice = (disk_trace[i + 1].get("decision") or {}).get("notice") or ""
            if "姓名=张三" in notice:
                restored_ok = True
                break
        check("批准提交后，页面回显的表单数据是填好的（不是空表单）", restored_ok)

        print("\n=== 8. 批准之后不再有幻影待审批 ===")
        resp = await client.get(f"/api/v1/tasks/{task_id}/approval", headers=ha)
        check("pending 变回 null", resp.json().get("pending") is None, str(resp.text))
        # meta.json 里那份快照不会被自动清理，接口能不能"无视它"才是关键
        disk = files.load_meta(task_id) or {}
        check("（前提）meta 里确实还留着旧快照", bool(disk.get("approval")))
        check("状态门挡住了旧快照", resp.json().get("pending") is None)
        # 任务已经不在 waiting_approval，再批必须被挡住（重复点击/前端重试）
        resp = await client.post(
            f"/api/v1/tasks/{task_id}/approval", json={"decision": "approve"}, headers=ha
        )
        check("任务已结束时再批 → 409", resp.status_code == 409, str(resp.status_code))

        print("\n=== 9. 拒绝 → 带着理由重新决策（P5a-4） ===")
        # 新提交一条同样的任务，这次拒绝它。
        # 为什么不复用上面那条：它已经跑完了，而"拒绝"验的是**停在半路时**的行为。
        resp = await client.post(
            "/api/v1/tasks",
            # 本脚本就是要测审批，显式 True（common.task_payload 默认 False 是给批处理的）
            json=task_payload(GOAL, START_URL, require_approval=True),
            headers=ha,
        )
        check("（拒绝链路）提交成功", resp.status_code == 200, resp.text)
        rej_id = resp.json()["task_id"]
        created_tasks.append(rej_id)
        print(f"    task_id = {rej_id}")

        meta = await wait_settled(client, rej_id, ha)
        check("（拒绝链路）停在 waiting_approval",
              meta.get("status") == "waiting_approval", str(meta.get("status")))
        pause_step = int((meta.get("approval") or {}).get("step_no") or 0)

        # 拒绝理由写成"一句能指导它改做法"的话，而不是"不行"。
        # 这条同时也是验收断言的一部分：它必须原样出现在下一轮的 prompt 里。
        REASON = "不要提交，直接告诉我页面上有哪些按钮"
        rejects = 0
        while rejects < MAX_APPROVALS and meta.get("status") == "waiting_approval":
            resp = await client.post(
                f"/api/v1/tasks/{rej_id}/approval",
                json={"decision": "reject", "note": REASON},
                headers=ha,
            )
            check(f"第 {rejects + 1} 次拒绝返回 200", resp.status_code == 200, resp.text)
            if resp.status_code != 200:
                break
            # 拒绝同样要重新入队：任务不能停在 waiting_approval 不动
            check(f"第 {rejects + 1} 次拒绝：任务回到 queued",
                  resp.json().get("task_status") == "queued", resp.text)
            rejects += 1
            meta = await wait_settled(client, rej_id, ha)

        check("拒绝之后不再停在等待（Agent 真的重新决策了）",
              meta.get("status") != "waiting_approval", str(meta.get("status")))
        check("拒绝链路没把任务搞成内部错误",
              meta.get("status") not in ("error", "bad_request", "nav_failed"),
              str(meta.get("status")))
        print(f"    一共拒了 {rejects} 次，最终状态 {meta.get('status')}")

        rows = await db_pending_rows(rej_id)
        check("审批项状态变成 rejected",
              bool(rows) and all(r[2] == "rejected" for r in rows), str(rows))
        check("拒绝理由落库（模型靠它重新决策）",
              all(r[5] == REASON for r in rows), str([r[5] for r in rows]))
        check("记下了是谁拒的", all(r[3] == a["user_id"] for r in rows), str(rows))

        rej_trace = files.load_trace(rej_id)
        rejected_rows = [r for r in rej_trace if r.get("rejected")]
        check("trace 里有 rejected 标记的行", len(rejected_rows) >= 1,
              str(len(rejected_rows)))
        # 这是 P5a-4 最要紧的一条：拒绝**绝不能**顺手执行那个动作。
        # 要是代码里把 reject 也走了一遍 _apply，用户会看到"我明明拒了，它还是点了提交"。
        check("被拒绝的动作没有执行过（ok=False）",
              all(not r.get("ok") for r in rejected_rows),
              str([(r["step"], r["ok"]) for r in rejected_rows]))
        check("整条任务没有任何 approved 执行行",
              not [r for r in rej_trace if r.get("approved")],
              str([r["step"] for r in rej_trace if r.get("approved")]))
        # 拒绝也是"接着跑"：前 N 步必须原样接上，不能从第 1 步重来
        check("拒绝后 trace 从第 1 步接续（没有重来）",
              rej_trace and rej_trace[0].get("step") == 1, str(rej_trace[:1]))
        check("第一次拒绝记在被暂停的那一步上",
              bool(rejected_rows) and rejected_rows[0]["step"] == pause_step,
              f"pause={pause_step} rejected={[r['step'] for r in rejected_rows]}")

        # 最有说服力的一条：理由真的进了**下一轮的 prompt**，而不只是躺在库里。
        # 读的是 trace 里冻结的 decision.rejection——它就是当时实际喂给模型的那段文字。
        # ⚠️ 注意看的是 rejection 而不是 notice：P5a-4 第一版把理由塞进一次性 notice，
        # 模型连续 5 轮照旧点提交（因为目标里就写着"点击提交按钮"，末尾一句观察压不住它）。
        # 现在理由被升到 [用户否决] 段、每轮都带，这条断言盯的就是那次修复。
        noticed = False
        for i, row in enumerate(rej_trace):
            if not row.get("rejected") or i + 1 >= len(rej_trace):
                continue
            text = (rej_trace[i + 1].get("decision") or {}).get("rejection") or ""
            if "被用户拒绝了" in text and "不要提交" in text:
                noticed = True
                break
        check("拒绝理由进了下一轮 prompt（[用户否决] 段）", noticed)

        # 这次不只是"没停住"，而是"真的换了个做法把任务做完了"。
        # 为什么敢断言 success：拒绝链路的价值全在这一条上——只做到"不卡住"
        # 而模型绕两圈还是去点提交，那功能等于没做（这正是第一版的样子）。
        check("拒绝之后模型真的换了做法并跑完（success）",
              meta.get("status") == "success", str(meta.get("status")))

        step_rows = await db_step_rows(rej_id)
        by_step = {int(s): int(c) for s, c in step_rows}
        check("暂停那一步有 2 条（暂停 + 被拒）",
              by_step.get(pause_step) == 2, str(by_step))

        resp = await client.get(f"/api/v1/tasks/{rej_id}/approval", headers=ha)
        check("（拒绝链路）之后再查 pending = null",
              resp.json().get("pending") is None, resp.text)

    print(f"\n=== 结果：{len(PASSED)} 通过 / {len(FAILED)} 失败 ===")
    if FAILED:
        for name in FAILED:
            print(f"  失败：{name}")
        print(f"\n⚠️ 保留了任务 {created_tasks} 便于排查")
        return 1

    # 全绿时清掉测试账号与任务，不留脏数据
    await cleanup(created_tasks, [email_a, email_b])
    print("已清理测试账号与任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
