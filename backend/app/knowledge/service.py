"""知识沉淀：任务产物 → knowledge_nodes。

N1 的职责边界：**只管"入"**（任务进、知识出）。检索(/)问答是 N3/N4 的事，
放在同文件会让"沉淀"和"查询"互相牵着改——沉淀要重跑全量历史时，
检索正在被前端高频调用，两者不该共用一个生命周期的改动节奏。

数据从哪来（这是最容易搞错的一环）：
- 任务级语料：`tasks` 表（goal / answer / start_url）——DB 里现成
- 页面级语料：`runs/<task_id>/trace.json` 最后一步的 `decision.snapshot.text`
  ——**不在 DB 里**。task_steps 只存了 thought 和截图路径，正文没入库。
  M5a 之前的任务没有 decision 字段 → 只能收到任务级，页面级跳过（计数不静默）。

为什么幂等（先删后插）：回填要能反复跑（改了切分方式就重来），
如果只 insert 不做清理，跑两次就是双份节点、检索时一条知识占两个 top-k 名额。
"""
from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import case, delete, select

from app.db.models import KnowledgeNode, Task
from app.db.session import SessionLocal
from app.knowledge.embedder import embed_query, get_embedder
from app.storage import files

log = logging.getLogger(__name__)

# 与 models.KnowledgeNode.source_type 的取值一一对应
SOURCE_TASK = "task_answer"   # 任务级：goal + 答案
SOURCE_PAGE = "page_text"     # 页面级：最终快照正文

# 页面正文少于这个字数就不收：太短的正文（"暂无数据"/纯导航）没有检索价值，
# 收了只会污染 top-k。50 字是经验值——测试站列表页正文约 200~400 字。
PAGE_MIN_CHARS = 50

# 切块参数（N2）。为什么不整页一条：
# 一条 3600 字的页面塞进一个向量 = 把 10 条名言揉成一个平均语义点，
# 检索"某句名言是谁说的"时，这个点离任何单句都远——长文本的语义被稀释了。
# 切块后每条名言有自己的向量，检索精度显著不同（这是 RAG 的常识，也是 p5 的经验）。
CHUNK_CHARS = 300    # 每块目标字数（约 2~4 行）
CHUNK_OVERLAP = 60   # 相邻块重叠字数（防答案正好跨切点）


def chunk_text(
    text: str, *, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """按行累加切块，尽量在行边界断开。

    为什么按行不按固定字数硬切：页面正文的行边界就是语义边界（一条名言一行、
    一个商品一行）。硬切会把 "by Albert Einstein" 切成 "by Albert" + "Einstein"，
    两块都不完整 —— 这正是 P1 踩过的坑（截断切断唯一答案，25%→75% 那条归因链）。

    重叠（overlap）：相邻块共享尾部若干字。代价是存储略大，收益是"答案横跨切点"
    时至少有一块包含完整答案。RAG 里这是标准做法。
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []

    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    for line in lines:
        buf.append(line)
        size += len(line) + 1  # +1 是换行符
        if size < max_chars:
            continue

        chunks.append("\n".join(buf))
        # 从尾部回溯，保留约 overlap 字符作为下一块的开头
        keep: list[str] = []
        acc = 0
        for prev in reversed(buf):
            keep.insert(0, prev)
            acc += len(prev) + 1
            if acc >= overlap:
                break
        # 单行本身超过 max_chars 时 keep == buf，直接清空避免同一行被重复 flush
        buf = keep if keep != buf else []
        size = sum(len(x) + 1 for x in buf)

    if buf:
        chunks.append("\n".join(buf))
    # 最后一块可能只剩重叠出来的残渣，过滤掉纯空白
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------- 纯组装（可单测）


def _content_hash(text: str) -> str:
    """内容指纹。用它判重而不是比 content：定长、可建索引、比较 O(1)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_nodes(
    *,
    task_id: str,
    goal: str,
    answer: str,
    start_url: str,
    trace: list[dict],
) -> list[dict]:
    """把任务产物组装成待写入的节点字典。

    纯函数（不碰 DB、不碰文件、不算向量）：输入→输出的映射，
    所以单测可以喂任意 shape 的 trace 验证组装逻辑，不用起数据库。
    向量化留到调用方批量做（一次 encode 多条比逐条快 3~5 倍）。
    """
    goal = (goal or "").strip()
    answer = (answer or "").strip()
    nodes: list[dict] = []

    # ---- 任务级：一问一答，检索"之前问过什么/答案是什么"时命中
    if goal:
        content = f"任务：{goal}\n答案：{answer or '（无答案）'}"
        nodes.append(
            {
                "task_id": task_id,
                "source_type": SOURCE_TASK,
                "url": start_url or "",
                "title": goal[:60],
                # 显式写"任务/答案"两个标签，是为了让向量靠近"问题-回答"这类语义；
                # 直接拼 "goal answer" 会让模型把它当成普通句子
                "content": content,
                "content_hash": _content_hash(content),
                "meta": {"goal": goal, "answer": answer},
            }
        )

    # ---- 页面级：最终快照正文，**切块后每块一个节点**（N2）
    for entry in reversed(trace or []):
        snapshot = ((entry.get("decision") or {}).get("snapshot") or {})
        text = (snapshot.get("text") or "").strip()
        if len(text) < PAGE_MIN_CHARS:
            continue
        chunks = chunk_text(text)
        total = len(chunks)
        for idx, chunk in enumerate(chunks):
            nodes.append(
                {
                    "task_id": task_id,
                    "source_type": SOURCE_PAGE,
                    # 用快照自己的 url（页面跳转后可能和 start_url 不同）
                    "url": snapshot.get("url") or start_url or "",
                    "title": (snapshot.get("title") or goal)[:60],
                    "content": chunk,
                    "content_hash": _content_hash(chunk),
                    "meta": {
                        "goal": goal,
                        "step_no": entry.get("step"),
                        "page_title": snapshot.get("title") or "",
                        # 块序号：排查"为什么这块检索不到"时要能定位到原文位置
                        "chunk_no": idx + 1,
                        "chunk_total": total,
                    },
                }
            )
        break  # 只收最后一个有正文的步骤（页面终态）

    return nodes


def _read_trace(task_id: str) -> list[dict]:
    """读 trace.json。老任务目录可能已被清理（磁盘上没了），返回空列表。"""
    path = files.RUNS_ROOT / task_id / "trace.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读 trace 失败 %s：%s", task_id, exc)
        return []


# ---------------------------------------------------------------- 写入（幂等）


async def flush_task(task_id: str) -> dict:
    """把一个任务沉淀成节点（先删该任务旧节点，再插入新节点）。

    返回 {"written": 节点数, "reason": 跳过原因}——调用方（回填/钩子）靠它统计。
    DB 不可用时抛异常给调用方决定（回填会记一条 error，不中断整批）。
    """
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return {"written": 0, "reason": "task_not_found"}

        trace = _read_trace(task_id)
        nodes = build_nodes(
            task_id=task.id,
            goal=task.goal,
            answer=task.answer,
            start_url=task.start_url,
            trace=trace,
        )
        if not nodes:
            return {"written": 0, "reason": "no_content"}

        # 幂等：清掉这个任务的旧节点再写新的一批
        await session.execute(
            delete(KnowledgeNode).where(KnowledgeNode.task_id == task_id)
        )

        # 入库判重：内容指纹已存在的跳过。
        # 场景：同一道题被 eval 跑多轮 → 内容逐字相同 → 只留最早那一条，
        # 溯源指向"第一次采集到它的任务"（语义上也更对）。
        # 注意顺序：必须先删自己旧节点再查重，否则同一任务重跑会被自己挡掉。
        hashes = [n["content_hash"] for n in nodes]
        existing = set(
            (
                await session.scalars(
                    select(KnowledgeNode.content_hash).where(
                        KnowledgeNode.content_hash.in_(hashes)
                    )
                )
            ).all()
        )
        fresh = [n for n in nodes if n["content_hash"] not in existing]
        if not fresh:
            return {"written": 0, "reason": "duplicate"}

        # 批量向量化：一次 encode 多条，比逐条 encode 省一次模型前向
        rows = await get_embedder().aencode([n["content"] for n in fresh])

        for node, vector in zip(fresh, rows):
            session.add(
                KnowledgeNode(
                    source_type=node["source_type"],
                    task_id=node["task_id"],
                    url=node["url"],
                    title=node["title"],
                    content=node["content"],
                    content_hash=node["content_hash"],
                    embedding=vector,
                    meta=node["meta"],
                    # M4 收尾：记下采集者。只记录不过滤（理由见 models.KnowledgeNode）。
                    # 从 tasks 表拿而不是从参数传：任务是 owner 的唯一真相来源，
                    # 回填历史数据时也能自动带上，不用调用方一个个记。
                    user_id=task.user_id,
                )
            )
        await session.commit()
        return {"written": len(fresh), "reason": ""}


async def backfill(limit: int | None = None) -> dict:
    """把历史成功任务批量沉淀。可反复跑（幂等）。

    只回填 status=success：失败任务的 answer 往往是兜底话术或空，
    入库后会把"任务：X / 答案：（无答案）"这种垃圾喂进检索结果。
    """
    stats = {"total": 0, "written": 0, "skipped": 0, "errors": 0}
    async with SessionLocal() as session:
        stmt = select(Task.id).where(Task.status == "success").order_by(Task.created_at)
        if limit:
            stmt = stmt.limit(limit)
        task_ids = list((await session.scalars(stmt)).all())

    stats["total"] = len(task_ids)
    for task_id in task_ids:
        try:
            result = await flush_task(task_id)
            if result["written"]:
                stats["written"] += result["written"]
            else:
                stats["skipped"] += 1
                log.debug("跳过 %s：%s", task_id, result["reason"])
        except Exception as exc:  # 单条失败不能带走整批
            stats["errors"] += 1
            log.warning("回填失败 %s：%s", task_id, exc)
    return stats


async def count_nodes() -> dict[str, int]:
    """按来源类型统计节点数（回填验收与 /stats 都可能用）。"""
    from sqlalchemy import func

    async with SessionLocal() as session:
        rows = await session.execute(
            select(KnowledgeNode.source_type, func.count(KnowledgeNode.id)).group_by(
                KnowledgeNode.source_type
            )
        )
        return {source: count for source, count in rows.all()}


# ---------------------------------------------------------------- 检索（N3）

# 来源加权（实测得出的必要修正）：
# 任务级节点的 content 是"任务：列出作者 / 答案：一堆名字"——语义极泛，
# 对任何"作者类"查询都高度相似（实测 24 条任务级节点把 top-3 全占了，
# 真正含细节的页面块排不进来，QA 检索命中率卡在 75%）。
# 页面级是一手内容，任务级是它的一层摘要——二手摘要不该压过原文。
# 0.85 是平衡点：强相关的摘要仍能进 top-k（比如问"做过哪些任务"），
# 但泛泛相关的摘要会让位给一手块。
SOURCE_WEIGHTS = {SOURCE_TASK: 0.85, SOURCE_PAGE: 1.0}


async def search(
    query: str,
    *,
    top_k: int = 5,
    source_type: str | None = None,
) -> list[dict]:
    """向量检索：按语义相似度返回节点。

    为什么用原生 SQLAlchemy 而不是 LlamaIndex 的 VectorStoreIndex：
    - 这条查询本身就是"一条带 ORDER BY 距离的 SELECT"，pgvector 的 `<=>` 算子
      已经把最核心的事做完了，LlamaIndex 在这之上加的只是"节点/检索器/查询引擎"
      三层包装——多一层黑盒，调不动的部分只能读它源码
    - 项目现有 repo.py 全是原生 SQLAlchemy，风格统一；面试被问"底层怎么实现的"
      能直接指着这条 SQL 讲，而不是"调了框架的接口"
    - 代价：以后要加混合检索（BM25 + 向量 RRF）得自己写，但那是 p6 已做过的活

    相似度口径：`1 - 余弦距离`，范围约 0~1（bge 归一化向量下）。
    不设最低阈值：bge-small-zh 对无关文本也能给 0.3~0.4 的相似度，
    拍脑袋定阈值会把边缘相关的真实答案挡掉；让 top_k 和调用方（/ask 的 prompt）
    去判断相关性，比硬阈值更稳。真要阈值，等评测数据（N4 的 QA 集）出来再定。
    """
    if not (query or "").strip():
        return []

    vector = await embed_query(query.strip())
    # <=> 是 pgvector 的余弦距离算子；距离越小越相似
    distance = KnowledgeNode.embedding.cosine_distance(vector)
    similarity = 1 - distance
    # 按来源加权后再排序（见 SOURCE_WEIGHTS 的说明）
    weight = case(SOURCE_WEIGHTS, value=KnowledgeNode.source_type, else_=1.0)
    score = similarity * weight

    stmt = (
        select(
            KnowledgeNode.task_id,
            KnowledgeNode.source_type,
            KnowledgeNode.title,
            KnowledgeNode.url,
            KnowledgeNode.content,
            KnowledgeNode.meta,
            similarity.label("similarity"),
            score.label("score"),
        )
        # 跳过没向量的节点（理论上不该有：flush 都带向量；
        # 但 SQLite 退化路径/手工插入可能留下 NULL，WHERE 兜一手）
        .where(KnowledgeNode.embedding.is_not(None))
        .order_by(score.desc())
        .limit(top_k)
    )
    if source_type:
        stmt = stmt.where(KnowledgeNode.source_type == source_type)

    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
        return [
            {
                "task_id": r.task_id,
                "source_type": r.source_type,
                "title": r.title,
                "url": r.url,
                "content": r.content,
                # similarity 是原始相似度（衡量"语义多接近"），
                # score 是加权后的排序分（衡量"该不该排在前面"）——两个都给，
                # 调排序时才能区分"模型认为不相关"和"被人为降权"
                "similarity": round(float(r.similarity), 4),
                "score": round(float(r.score), 4),
                "meta": r.meta,
            }
            for r in rows
        ]
