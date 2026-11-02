"""任务接口：提交 / 查状态 / 取轨迹。三个就够跑通闭环。

设计上的两个刻意选择：
1. 提交接口**不做校验以外的活**：不跑任务、不等结果，只建记录 + 返回 id（否则前端卡 20 秒）
2. 白名单在这里也查一次：submit 是外部输入的第一个入口，
   等进到浏览器层才拦（session._assert_host）错误会晚 5 秒才出现，用户会觉得"点了没反应"

M4 之后所有路由都挂 Depends(get_current_user)：
- 认证开启：没 token 401，别人的任务 404（assert_owned 统一判定）
- 认证关闭：user 为 None，行为与 M3 完全一致（本地单人模式）
"""
from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.deps import assert_owned, get_current_user
from app.config import settings
from app.db import repo
from app.db.models import User
from app.runner import task_runner
from app.storage import files

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class SubmitTaskRequest(BaseModel):
    """提交任务的最小入参。上限是为了挡住误粘贴的超长文本（会把 prompt 撑爆）。"""

    goal: str = Field(min_length=1, max_length=500, description="要 Agent 做的事")
    start_url: str = Field(min_length=1, max_length=500, description="从哪个页面开始")
    # P5a：这个任务要不要走审批闸门。默认要（默认安全）。
    # 批处理/压测/采集脚本传 false，否则一旦触发审批就会永远卡在 waiting_approval。
    # 注意它是**任务级**的：执行在 worker 进程，客户端改环境变量影响不到它。
    require_approval: bool = Field(
        default=True, description="高风险动作是否需要人工批准（批处理脚本传 false）"
    )
    # P5b：用哪一份保存好的登录态开局。None = 匿名访问（和 P5b 之前完全一样）。
    # 注意这里**只有 id，没有账号密码**：密码在保存那一刻就用完丢了，
    # 这个接口永远收不到密码，也就不可能把它写进日志或传给模型。
    credential_id: int | None = Field(
        default=None, description="用哪份登录态（先调 /credentials 保存）；空表示匿名"
    )
    # P6a：任务级视觉强制开关。默认关——视觉是增强项，且会改 prompt，
    # 批量评测不该被它悄悄影响。canvas 演示/对比评测时传 true。
    # ⚠️ 它和进程级 VISION_ENABLED 是"与"关系：总闸关着时这里传 true 也不看
    #（总闸是对所有任务生效的止血闸，见 workflow.__init__ 的说明）。
    vision: bool = Field(
        default=False, description="强制启用视觉兜底（canvas 演示/评测对比用）"
    )


def assert_allowed_url(url: str) -> None:
    """URL 必须是 http(s) 且域名在白名单内。

    P5b 起这个函数被 `api/credentials.py` 复用（保存登录态时也要校验登录页地址）。
    为什么不各写一份：白名单是**安全边界**，两份实现早晚漂移，
    而漂移的那一份会让"能不能访问这个站"出现两个答案——那是最难查的一类问题。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="start_url 必须以 http:// 或 https:// 开头")
    host = (parsed.hostname or "").lower()
    if host not in settings.allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=f"域名 {host} 不在白名单（允许：{'、'.join(settings.allowed_hosts)}）；不允许访问外部站点",
        )


@router.post("", summary="提交任务（立即返回 task_id，后台执行）")
async def create_task(
    req: SubmitTaskRequest,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    goal = req.goal.strip()
    start_url = req.start_url.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal 不能为空")
    assert_allowed_url(start_url)

    if req.credential_id is not None:
        await _assert_credential_usable(req.credential_id, user, start_url)

    task_id = await task_runner.submit(
        goal=goal,
        start_url=start_url,
        user_id=user.id if user else None,  # None = 认证关闭 → repo 挂占位用户
        require_approval=req.require_approval,
        credential_id=req.credential_id,
        vision=req.vision,
    )
    return {"task_id": task_id, "status": "queued"}


async def _assert_credential_usable(
    credential_id: int, user: User | None, start_url: str
) -> None:
    """提交时就把"这份登录态根本用不了"的情况挡掉。

    为什么要在这里挡、而不是等 worker 解密时才发现：
    任务一旦跑起来，用户看到的是"Agent 在未登录状态瞎逛 30 秒然后失败"，
    而他真正的错误（选错了登录态 / 那是别人的）已经被埋在过程里了。
    提交时一句话报出来，是最省时间的失败。

    三道检查，代价从低到高：
    1. 必须有登录用户 —— 登录态是凭证，没有归属人就不该存在（也不会存在）
    2. 必须是自己那份 —— 不匹配一律 404，理由同 credentials.delete_credential
    3. 站点要能对上 —— 见下面，这条只提示不拦
    """
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="认证关闭模式下没有可用的登录态（登录态需要归属人）",
        )
    row = await repo.fetch_credential(credential_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这份登录态")

    # 站点不一致**只提示不拒绝**：SSO 是真实存在的（sso.a.com 登录、跳到 app.a.com），
    # 硬拦会误伤它；而 cookie 的域由浏览器自己判，注入错了也只是"没生效"，不会串号。
    # 但一定要说一句，否则"我明明选了登录态，它怎么还是未登录"要查很久。
    host = (urlparse(start_url).hostname or "").lower()
    if row["site"] and row["site"] != host:
        log.warning(
            "登录态站点与任务起始站点不一致：credential=%s site=%s start=%s（不阻断）",
            credential_id,
            row["site"],
            host,
        )


@router.get("", summary="列出我的任务（按创建时间倒序）")
async def list_tasks(
    user: Annotated[User | None, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="跳过条数"),
) -> dict:
    """只返回当前登录用户自己的任务。

    M4 收尾补的接口。在此之前前端只有 /stats 的聚合数字，没有"我的任务列表"——
    隔离做没做，用户其实看不见也验不了。有了它，换个账号登录就能直观确认：
    同一个数据库里，A 列表里有 3 个任务、B 列表是空的。

    降级与 /stats 一致：数据库不可用 → 200 + available=false + 空列表，
    不把"没装数据库"表现成 5xx（P3 起就是这个口径）。
    """
    if not await repo.ping():
        return {"available": False, "note": "数据库不可用，无法列出任务", "items": [], "total": 0}
    data = await repo.fetch_task_list(
        user_id=user.id if user else None, limit=limit, offset=offset
    )
    return {"available": True, "limit": limit, "offset": offset, **data}


@router.get("/{task_id}", summary="查任务状态与结论")
async def get_task(
    task_id: str,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    # 这里必须是 async def：底层要查数据库（异步驱动），同步 def 上取不到结果
    # 归属校验 + "不存在 → 404" 都在 assert_owned 里，这里不需要再判一次
    return assert_owned(await task_runner.get_task(task_id), user)


@router.get("/{task_id}/trace", summary="取完整轨迹（含每步截图地址）")
async def get_trace(
    task_id: str,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    # 归属校验和 get_task 共用一条规则：别人的任务在这里也是 404，
    # 不能靠 trace 接口绕过隔离摸到别人的执行过程
    assert_owned(await task_runner.get_task(task_id), user)

    items = []
    for item in await task_runner.get_trace(task_id):
        row = dict(item)
        # 磁盘上只有文件名，前端要的是能直接 <img src> 的地址
        if row.get("screenshot"):
            row["screenshot_url"] = files.screenshot_url(task_id, row["screenshot"])
        items.append(row)
    return {"task_id": task_id, "trace": items}
