"""知识检索接口（P4-N3）。

一个只读端点，把 pgvector 的相似度检索暴露给前端和调试用。
问答接口（/ask，N4）会复用同一个 `service.search`，区别只在"检索之后要不要
再走一次 LLM 生成"。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.deps import get_current_user
from app.db import repo
from app.db.models import KnowledgeNode, User
from app.db.session import SessionLocal
from app.knowledge import qa, service

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


async def _task_in_knowledge(task_id: str) -> bool:
    """这个任务是否被知识库收录过（有没有它的节点）。DB 不可用就当没有。"""
    try:
        async with SessionLocal() as session:
            found = await session.scalar(
                select(KnowledgeNode.id).where(KnowledgeNode.task_id == task_id).limit(1)
            )
        return found is not None
    except Exception:
        return False


@router.get("/search", summary="语义检索已沉淀的知识")
async def search_knowledge(
    q: str = Query(min_length=1, max_length=200, description="自然语言查询"),
    top_k: int = Query(default=5, ge=1, le=20, description="返回条数"),
    source_type: str | None = Query(
        default=None,
        description="按来源过滤：task_answer（任务问答）/ page_text（页面正文）",
    ),
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> dict:
    """检索知识节点。

    降级语义与 /stats 一致：检索是增强功能，出问题返回 200 + available=false，
    不让前端走异常分支。数据库没起、模型没下载完，都不该表现成 5xx。

    为什么首次请求会慢 1~2 秒：embedding 模型首次调用才加载（懒加载）。
    这是刻意的——启动时加载会让 /health 迟迟不通；而检索是低频操作，
    第一次慢一点换启动快，划算。要消掉这个首响延迟，可以在 lifespan 里
    预热（像 ASR 那样），但 P4 不必要。
    """
    try:
        results = await service.search(q, top_k=top_k, source_type=source_type)
    except Exception as exc:  # DB 不可用 / 模型加载失败，都降级而不是 500
        return {
            "available": False,
            "note": f"检索暂不可用：{exc}",
            "q": q,
            "count": 0,
            "results": [],
        }
    return {"available": True, "q": q, "count": len(results), "results": results}


class AskRequest(BaseModel):
    """问答入参。上限 300 字：再长的"问题"多半是粘贴了整段文本，不适合当查询。"""

    question: str = Field(min_length=1, max_length=300, description="自然语言问题")
    top_k: int = Field(default=5, ge=1, le=10, description="喂给模型的材料条数")


@router.post("/ask", summary="知识库问答：检索材料 + LLM 生成带引用的回答")
async def ask_knowledge(
    req: AskRequest,
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> dict:
    """检索 top-k 材料 → 组 prompt → 模型生成 → 返回答案 + 来源列表。

    返回里的 sources 下标与答案中的 [n] 引用一一对应，前端据此做"点引用跳原任务"。

    降级：检索或 LLM 失败都返回 200 + available=false，
    与 /search、/stats 保持一致（问答是增强功能，不该让前端走异常分支）。
    """
    try:
        result = await qa.ask(req.question, top_k=req.top_k)
    except Exception as exc:
        return {
            "available": False,
            "note": f"问答暂不可用：{exc}",
            "answer": "",
            "sources": [],
            "hits": [],
        }
    return {"available": True, **result}


@router.get("/tasks/{task_id}", summary="读取知识来源任务的结论与轨迹（只读，供溯源核对）")
async def get_knowledge_task(
    task_id: str,
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> dict:
    """点来源时打开原任务。

    和 `/api/v1/tasks/{id}` 的两点区别（都是刻意的）：

    1. **不做归属校验**。知识库本身是全局共享的（P4 没做知识级隔离），
       它的来源如果用"我是谁的任务"去卡，非采集账号点任何一个来源都是 404
       ——验收线"来源可点回原始任务"就废了。
    2. **但要求任务真的被知识库收录过**（knowledge_nodes 里有节点）。
       没有这条约束，这个接口就成了"绕过归属校验读任意任务"的后门。
       收录过 = 它的内容已经进入共享知识库，再读一次轨迹不增加暴露面。
    """
    if not await _task_in_knowledge(task_id):
        raise HTTPException(status_code=404, detail="该任务不在知识库中，无法溯源")
    meta = await repo.fetch_task(task_id)  # user_id=None：不过滤归属
    if meta is None:
        raise HTTPException(status_code=404, detail="任务数据不可用（数据库没起或已被清理）")
    trace = await repo.fetch_steps(task_id)
    # readonly 标记：前端据此禁止对它做"语音/继续操作"等有副作用的动作
    return {"meta": meta, "trace": trace, "readonly": True}
