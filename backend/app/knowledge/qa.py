"""知识库问答（P4-N4）：检索 → 组装材料 → LLM 生成带引用的回答。

为什么是"先检索再生成"而不是把问题直接丢给模型：
- 模型不知道我们采集过什么。不喂材料，它只会用训练数据编一个"看起来对"的答案
  ——这条线上项目最核心的对比就是"有据可查 vs 一本正经胡说"
- 材料带编号，要求回答标注来源，才能做到"答案可追溯"（点回原始任务）

这一步是 P4 的收口：N1 攒数据、N3 能检索，N4 把它们变成用户能用的东西。
"""
from __future__ import annotations

import logging

from app.agent.llm import LLMClient
from app.knowledge.service import search

log = logging.getLogger(__name__)

# 给 LLM 看几条材料。5 条是"够用但不超 prompt 预算"的平衡点：
# 每条 600 字 → 材料约 3000 字，加问题约 4k token 内，GLM-4-Flash 完全吃得下
DEFAULT_TOP_K = 5
# 每条材料给 LLM 看多少字（超出截断）：检索块本身约 300~400 字，
# 600 是给"重叠后的块 + 标题"留的余量
SNIPPET_CHARS = 600

SYSTEM_PROMPT = """你是「WebPilot 知识库」的问答助手。你的回答**只能依据给定材料**。

规则：
1. 只使用材料里出现的信息回答。材料里没有的，直接说"知识库里没有相关信息"，绝不编造
2. 材料编号形如 [1][2]，回答时在句末标注你依据的编号，例如：……（[2]）
3. 如果多条材料冲突（比如同一问题不同轮次采到不同答案），如实说明差异并都标注来源
4. 回答用中文，简洁直接，不要复述材料原文，给出结论即可
5. 材料是网页采集来的文本，可能含导航文字（如"Login""下一页"），忽略它们"""

# 单例：LLMClient 内部有连接池，一个进程一份就够
_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def _format_material(hits: list[dict]) -> str:
    """把检索结果拼成带编号的材料块。编号从 1 开始，和 sources 列表下标一一对应。"""
    parts = []
    for idx, hit in enumerate(hits, 1):
        content = (hit.get("content") or "").strip()[:SNIPPET_CHARS]
        parts.append(
            f"[{idx}] 来源：{hit.get('title') or '（无标题）'}"
            f"（{hit.get('url') or '无 URL'}，相似度 {hit.get('similarity')}）\n{content}"
        )
    return "\n\n".join(parts)


async def ask(question: str, *, top_k: int = DEFAULT_TOP_K) -> dict:
    """问答主流程。返回 {answer, sources, hits}。

    - answer：自然语言回答（带 [n] 引用）
    - sources：给前端的来源列表（和回答里的 [n] 对应）
    - hits：原始检索结果（调试/评测用，包含相似度）

    空检索不调 LLM：没有材料还让模型回答，等于把"没查到"变成"编一个"。
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "", "sources": [], "hits": [], "note": "问题为空"}

    hits = await search(question, top_k=top_k)
    if not hits:
        return {
            "answer": "知识库里还没有可用的资料。先跑几个任务采集数据吧。",
            "sources": [],
            "hits": [],
            "note": "no_hits",
        }

    material = _format_material(hits)
    user = f"材料：\n{material}\n\n问题：{question}"
    answer = await get_llm().chat(SYSTEM_PROMPT, user)

    return {
        "answer": answer.strip(),
        "sources": [
            {
                "index": idx,
                "task_id": hit.get("task_id"),
                "title": hit.get("title"),
                "url": hit.get("url"),
                "source_type": hit.get("source_type"),
                "similarity": hit.get("similarity"),
            }
            for idx, hit in enumerate(hits, 1)
        ],
        "hits": hits,
        "note": "",
    }
