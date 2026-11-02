"""统计接口（M5b）：一个只读端点，把数据库里的任务聚合给前端和评测脚本。

为什么独立一个文件：stats 不是"某个任务"的操作，塞进 tasks.py 会把
`/api/v1/tasks` 的语义搅浑。单独一个 router，prefix 也不同（/api/v1/stats）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_user
from app.db import repo
from app.db.models import User

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("", summary="任务聚合统计（成功率 / 步数 / 耗时分位 / token / 状态分布）")
async def get_stats(
    since: str | None = Query(
        default=None,
        description="只统计此时间之后提交的任务（ISO 8601，如 2026-10-25T10:00:00）",
    ),
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> dict:
    """聚合统计。分母只含终态任务，排队/运行中的不算。

    M4：统计按登录用户隔离——看到别人的任务数混进自己的成功率，
    比"看不到"更糟。认证关闭时 user 为 None，行为与 M5b 一致（全量）。

    降级语义：DB 连不上时返回 200 + available=false，而不是 503——
    统计是增强功能，前端拿到 false 显示一句提示即可，不必走异常分支。
    """
    # 字符串 → datetime 的解析放在路由层（DB 层不该兼职处理用户输入格式）。
    # 不带时区的时间按本地时区理解——和 meta.json 里 strftime 的时间口径一致。
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.astimezone()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"since 不是合法的 ISO 时间：{since!r}（示例：2026-10-25T10:00:00）",
            )

    data = await repo.fetch_stats(since_dt, user_id=user.id if user else None)
    if data is None:
        return {
            "available": False,
            "note": "数据库不可用，统计暂不可用（任务本身照常可跑）",
        }
    return {"available": True, "since": since, **data}
