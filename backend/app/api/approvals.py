"""审批接口：查当前待审批项（P5a-2）+ 提交人的决定（P5a-3 批准 / P5a-4 拒绝）。

为什么单独一个文件而不是塞进 tasks.py：
"提交决策 + 重新入队恢复"是带状态机、带回滚的独立逻辑；塞进 tasks.py 会让那个文件
从 127 行涨到 300+，而且两种职责（跑任务 / 处理人的决策）混在一个 router 里，
改一处要读全篇。

prefix 和 tasks.py 相同不会冲突：FastAPI 按**完整路径**匹配，
`/api/v1/tasks/{task_id}` 只有两段，匹配不到 `/api/v1/tasks/x/approval` 这种三段路径。

和任务接口共用同一条归属规则（assert_owned）：
B 用自己的 token 查 A 的待审批 → 404，和查 A 的任务详情表现完全一致。
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.deps import assert_owned, get_current_user
from app.db import repo
from app.db.models import User
from app.runner import task_runner
from app.status import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    PAUSED,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["approvals"])


def _from_meta(raw: dict) -> dict:
    """把 P5a-1 写进 meta.json 的那份待审批信息补齐成 API 形状。

    为什么需要这层：meta 里的那份是 workflow 直接吐出来的（step_no / reason /
    action / snapshot_url / context 五键），没有表才有的 id、created_at、
    waiting_s。前端只认一种形状，所以在这里补空值，而不是让前端写两套判断。

    这条回退路径不是历史包袱，它有两个真实用途：
    1. 数据库不可用的机器（db_usable() == False）——审批卡片照样要能显示
    2. approvals 表写入失败（DB 抖动）——任务该停还是停了，人得知道在等什么
    """
    return {
        # 表写成功时 on_approval 会把 id 一起塞进 meta，这里原样透传；
        # 表写失败时 meta 里就没有 id，透传出来自然也是 None。
        # 为什么不在 meta 有 id 时抛弃它：P5a-3 的批准要按 id 定位"批的是哪一条"，
        # 有 id 才能保证批的确实是当前那条（同一步被拒两次时 step_no 会重复）。
        "id": raw.get("id"),
        "task_id": raw.get("task_id"),
        "step_no": raw.get("step_no"),
        "action": raw.get("action") or {},
        "reason": raw.get("reason") or "",
        "snapshot_url": raw.get("snapshot_url") or "",
        "context": raw.get("context") or {},
        "status": APPROVAL_PENDING,
        # meta 里没有创建时间，等待时长算不出来——显示成"未知"比编一个数字好
        "created_at": None,
        "waiting_s": None,
        "note": "",
        "decided_by": None,
    }


@router.get("/{task_id}/approval", summary="查该任务当前的待审批项")
async def get_approval(
    task_id: str,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """前端轮询用：任务进入 waiting_approval 后，靠它渲染"Agent 想做什么"。

    返回 `{"pending": null}` 表示没有待审批项——这是个**正常**结果（任务可能已经
    被批过/拒过、或者压根没触发过闸门），不报 404；真正不存在的任务由
    assert_owned 报 404，两者必须能区分开：
    前者是"任务存在但没东西要批"，后者是"你根本不该知道这个 id 存在"。
    """
    meta = assert_owned(await task_runner.get_task(task_id), user)

    # 先查表（P5a-2 之后的常态），查不到再退回 meta.json（P5a-1 的单机形态）
    pending = await repo.fetch_pending_approval(task_id)
    if pending is None and meta.get("status") in PAUSED:
        # ⚠️ 回退必须加状态门：meta.json 里的 approval 是"暂停那一刻"的快照，
        # 批准/拒绝之后**没有人去清它**（P5a-3 续跑会覆盖，但那是另一条路径）。
        # 不加这个判断，任务已经跑完了、approvals 表里早就 approved，
        # 这个接口还会返回一条幻影 pending，前端于是永远挂着审批卡片。
        # 判断条件用 PAUSED 而不是"表里没有 pending"：只有任务停在
        # waiting_approval 时，"meta 里有待审批信息"才是有意义的事实。
        raw = meta.get("approval")
        pending = _from_meta(raw) if raw else None

    return {
        "task_id": task_id,
        # 顺便回传任务状态：前端可以据此判断"暂停中是还在等，还是已经续跑/结束了"，
        # 省掉一次 GET /tasks/{id}
        "status": meta.get("status"),
        "pending": pending,
    }


class DecisionRequest(BaseModel):
    """一次审批决定。"""

    decision: Literal["approve", "reject"] = Field(description="approve 批准 / reject 拒绝")
    # 审批意见。拒绝时它是**有语义的输入**（会回喂给模型，让它换个做法），
    # 批准时它只是审计备注。上限 500 字：再长也进不了 prompt。
    note: str = Field(default="", max_length=500, description="审批意见（拒绝理由会回喂模型）")


@router.post("/{task_id}/approval", summary="批准/拒绝待审批动作（两者都会重新入队续跑）")
async def decide_approval(
    task_id: str,
    req: DecisionRequest,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """人做出的决定。**两种决定都会让任务接着跑**，区别只在"到了现场做不做那个动作"：

    - 批准 → 回到暂停页，执行那个动作，然后继续
    - 拒绝 → 回到暂停页，**不执行**，把理由回喂模型让它换个做法

    为什么拒绝也要续跑而不是把任务判死：人否的是**这个动作**，不是整个任务。
    判死的话用户就只有"批"和"白跑一遍"两个选项，久了必然闭眼点批准——
    闸门从"我审一下"退化成橡皮图章，等于白做。

    状态机（每一步失败都要能说清后果）：

        任务 waiting_approval + 审批项 pending
              │ POST approve / reject
              ▼
        UPDATE approvals WHERE status='pending'  ← 一次 compare-and-swap
              │ rowcount==1（抢到了）
              ▼
        任务 queued → 入队 resume job（worker 按审批项终态决定执行 or 重新决策）
              │ 入队失败
              ▼
        审批项退回 pending + 任务退回 waiting_approval ← 用户可重试

    为什么用 409 而不是 400 表达"没有待审批项"：
    这不是请求格式错，而是**当前状态不允许这个操作**（已经处理过了/已经续跑了）。
    409 让前端知道"刷新一下看看最新状态"，而不是"我的参数写错了"。
    """
    # 顺序很重要：先判归属（404），再判状态（409）。
    # 反过来的话，别人能通过 409/403 的差异探出"这个 task_id 存在"。
    meta = assert_owned(await task_runner.get_task(task_id), user)

    if meta.get("status") not in PAUSED:
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态是 {meta.get('status')}，没有在等待审批",
        )

    pending = await repo.fetch_pending_approval(
        task_id, user_id=user.id if user else None
    )
    if pending is None:
        raise HTTPException(
            status_code=409,
            detail="没有待审批项：可能已经被处理过（刷新看看最新状态）",
        )

    # 人的决定 → 审批项的目标状态。两种决定共用下面这段 CAS + 入队：
    # 差别只有这一个字符串，剩下的（抢锁、推状态、失败回滚）完全一样，
    # 复制成两段等于同一处逻辑有两份，改一处忘一处时最难查。
    target = APPROVAL_APPROVED if req.decision == "approve" else APPROVAL_REJECTED

    # compare-and-swap：两个请求同时点，只有一个能改到（另一个 rowcount=0 → 409）
    won = await repo.decide_approval(
        pending["id"],
        status=target,
        decided_by=user.id if user else None,
        note=req.note,
    )
    if not won:
        raise HTTPException(status_code=409, detail="这一条审批已被处理过（可能是并发点击）")

    try:
        await task_runner.resume(task_id, pending["id"])
    except Exception as exc:
        # 入队失败：把审批项退回 pending，让人能再点一次。
        # 不回滚的话就是那个最难查的死法——界面上一切正常，任务永远不动。
        log.warning(
            "任务 %s 提交审批决定（%s）后入队失败，回滚审批项 %s：%s",
            task_id, req.decision, pending["id"], exc,
        )
        await repo.revert_approval(pending["id"])
        raise HTTPException(
            status_code=503, detail="决定已记录，但重新入队失败（队列不可用），请稍后重试"
        ) from exc

    return {
        "task_id": task_id,
        "approval_id": pending["id"],
        "decision": req.decision,
        "task_status": "queued",
        # 前端拿到这条就能刷新了：任务已重新排队，接着轮询 /tasks/{id}
        "note": (
            "已批准，任务正在重新排队执行"
            if req.decision == "approve"
            else "已拒绝，Agent 会带着这条理由重新决策"
        ),
    }
