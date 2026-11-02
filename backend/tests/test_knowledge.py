"""知识沉淀测试（P4-N1）。

两层策略，和项目其它模块一致：
- **纯函数层**（build_nodes）：不碰 DB、不碰模型，喂任意 trace 验证组装逻辑。
  这一层覆盖最多——页面级怎么选、短正文怎么滤、hash 怎么算，都是容易写错的地方。
- **真库层**（flush_task）：验证幂等和跨任务去重，需要 PostgreSQL + pgvector，
  没起库时自动跳过。向量化用 FakeEmbedder 桩掉，避免每条测试都加载 95MB 模型
  （真模型只加载一次就要 1~2 秒，十条测试就是 20 秒，测试会慢到没人愿意跑）。
"""
from __future__ import annotations

import json
import socket

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine.url import make_url

from app.config import settings
from app.db import repo
from app.db.models import KnowledgeNode, Task
from app.db.session import SessionLocal, engine
from app.knowledge import service
from app.storage import files


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """每个测试前清空连接池（与 test_stats 同款，原因见那边的注释）。

    pytest-asyncio 每个测试新建事件循环，池里的旧连接绑在旧循环上，
    复用时 asyncpg 收尾会抛 "Event loop is closed"。
    """
    await engine.dispose()
    yield


# ---------------------------------------------------------------- 造数工具


def _snapshot(text: str = "", title: str = "页面标题", url: str = "http://x/list.html") -> dict:
    """造一个与 workflow 落盘形状一致的快照。"""
    return {"url": url, "title": title, "text": text, "elements": [], "headings": []}


def _entry(step: int, *, text: str = "", title: str = "页面标题", legacy: bool = False) -> dict:
    """一行 trace。legacy=True 造 M5a 之前的老格式（没有 decision 字段）。"""
    row = {"step": step, "url": "http://x/", "thought": "t", "action": "click e1", "ok": True}
    if not legacy:
        row["decision"] = {"snapshot": _snapshot(text, title), "snapshot_text": "[URL] http://x/"}
    return row


LONG_TEXT = "这是一段足够长的页面正文，用来通过最小长度过滤。" * 4  # > 50 字


# ---------------------------------------------------------------- 纯函数层


def test_build_task_node_content_and_hash():
    nodes = service.build_nodes(
        task_id="t1",
        goal="找出价格低于 100 的商品",
        answer="无线鼠标（¥89）",
        start_url="http://x/list.html",
        trace=[],
    )
    assert len(nodes) == 1
    node = nodes[0]
    assert node["source_type"] == service.SOURCE_TASK
    assert node["content"] == "任务：找出价格低于 100 的商品\n答案：无线鼠标（¥89）"
    assert node["title"] == "找出价格低于 100 的商品"
    assert node["url"] == "http://x/list.html"
    # sha256 十六进制固定 64 字符
    assert len(node["content_hash"]) == 64


def test_empty_answer_gets_placeholder():
    nodes = service.build_nodes(task_id="t1", goal="g", answer="", start_url="u", trace=[])
    assert "（无答案）" in nodes[0]["content"]


def test_build_page_node_from_last_step_with_text():
    """页面级取**最后一个**带正文的步骤（页面已翻到终态，正文才是采集到的东西）。

    N2 起正文会被切块，一页可能产出多块——所以断言"所有页面级块都来自 step 2"，
    而不是"恰好一块"。
    """
    trace = [
        _entry(1, text="第一页正文" + LONG_TEXT, title="第一页"),
        _entry(2, text=LONG_TEXT, title="最终页"),
    ]
    nodes = service.build_nodes(task_id="t1", goal="g", answer="a", start_url="u", trace=trace)
    page = [n for n in nodes if n["source_type"] == service.SOURCE_PAGE]
    assert page, "应有页面级节点"
    assert all(n["meta"]["step_no"] == 2 for n in page)   # 全部来自最后一步
    assert all(n["title"] == "最终页" for n in page)
    assert all(n["meta"]["chunk_no"] >= 1 for n in page)


# ---------------------------------------------------------------- 切块（N2）


def test_chunk_short_text_single_chunk():
    chunks = service.chunk_text("第一行\n第二行")
    assert len(chunks) == 1
    assert chunks[0] == "第一行\n第二行"


def test_chunk_empty_text():
    assert service.chunk_text("") == []
    assert service.chunk_text("   \n  \n") == []


def test_chunk_long_text_splits_at_line_boundary():
    """20 行 × 40 字 ≈ 800 字，按 300 字切应得到 3 块左右，且不切断行。"""
    lines = [f"第{i:02d}行的内容" + "字" * 30 for i in range(20)]
    chunks = service.chunk_text("\n".join(lines), max_chars=300, overlap=60)
    assert 2 <= len(chunks) <= 6
    # 每块都必须由完整行组成（不能把一行切断）
    for c in chunks:
        for ln in c.splitlines():
            assert ln.strip().endswith("字") or ln.startswith("第")


def test_chunk_overlap_makes_adjacent_chunks_share_text():
    """相邻块要共享尾部内容（答案横跨切点时至少一块完整）。"""
    lines = [f"行{i}-" + "内容" * 20 for i in range(10)]
    chunks = service.chunk_text("\n".join(lines), max_chars=200, overlap=80)
    assert len(chunks) >= 2
    # 第 1 块的最后一行，应该出现在第 2 块开头附近
    last_line_of_first = chunks[0].splitlines()[-1]
    assert last_line_of_first in chunks[1]


def test_chunk_single_huge_line_terminates():
    """单行超过 max_chars 不能死循环（内部有 keep==buf 的保护分支）。"""
    chunks = service.chunk_text("超长单行" * 200, max_chars=100, overlap=20)
    assert len(chunks) == 1          # 只有一行，切不开，就一块
    assert chunks[0].startswith("超长单行")


def test_short_page_text_skipped():
    nodes = service.build_nodes(
        task_id="t1", goal="g", answer="a", start_url="u", trace=[_entry(1, text="太短")]
    )
    assert all(n["source_type"] != service.SOURCE_PAGE for n in nodes)
    assert len(nodes) == 1  # 只剩任务级


def test_legacy_trace_without_decision():
    """M5a 之前的老 trace：没有 decision，但任务级照收。"""
    trace = [_entry(1, legacy=True), _entry(2, legacy=True)]
    nodes = service.build_nodes(task_id="t1", goal="g", answer="a", start_url="u", trace=trace)
    assert [n["source_type"] for n in nodes] == [service.SOURCE_TASK]


def test_no_goal_only_page_node():
    nodes = service.build_nodes(
        task_id="t1", goal="  ", answer="", start_url="u", trace=[_entry(1, text=LONG_TEXT)]
    )
    assert [n["source_type"] for n in nodes] == [service.SOURCE_PAGE]


def test_hash_stable_and_discriminative():
    """同内容 hash 必须一致（去重依赖它），不同内容必须不同（防误杀）。"""
    a1 = service.build_nodes(task_id="t1", goal="g", answer="a", start_url="u", trace=[])
    a2 = service.build_nodes(task_id="t2", goal="g", answer="a", start_url="u", trace=[])
    b = service.build_nodes(task_id="t3", goal="g", answer="b", start_url="u", trace=[])
    assert a1[0]["content_hash"] == a2[0]["content_hash"]
    assert a1[0]["content_hash"] != b[0]["content_hash"]


# ---------------------------------------------------------------- 真库层


def _pg_reachable() -> bool:
    url = make_url(settings.database_url)
    try:
        with socket.create_connection((url.host or "127.0.0.1", url.port or 5432), timeout=0.5):
            return True
    except OSError:
        return False


needs_db = pytest.mark.skipif(
    not _pg_reachable(), reason="PostgreSQL 未启动（wp-postgres 容器）；真库测试跳过"
)


class _FakeEmbedder:
    """桩掉真模型：返回固定 512 维向量。

    测试关心的是"写入/去重/幂等"这些逻辑，不关心向量算得准不准
    （向量质量由检索实测覆盖）。桩掉后每条真库测试从 ~1.5s 降到 ~10ms。
    """

    dim = 512

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 512 for _ in texts]

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        return self.encode(texts)


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """把 RUNS_ROOT 指到临时目录 + 桩掉 embedding 模型。"""
    monkeypatch.setattr(files, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(service, "get_embedder", lambda: _FakeEmbedder())
    return tmp_path


def _write_trace(root, task_id: str, trace: list[dict]) -> None:
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "trace.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")


async def _make_task(task_id: str, goal: str) -> None:
    ok = await repo.create_task(
        task_id=task_id, goal=goal, start_url="http://127.0.0.1:8000/testsite/", model="glm-4-flash"
    )
    assert ok, "造数失败：确认 PG 容器与 alembic upgrade head 已执行"


async def _cleanup(*task_ids: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(KnowledgeNode).where(KnowledgeNode.task_id.in_(task_ids)))
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        await session.commit()


@needs_db
async def test_flush_is_idempotent(fake_env):
    """同一个任务 flush 两次，节点数不翻倍（回填要能反复跑）。"""
    tid = "kn-test-idem"
    await _make_task(tid, "幂等测试任务")
    _write_trace(fake_env, tid, [_entry(1, text=LONG_TEXT)])
    try:
        first = await service.flush_task(tid)
        second = await service.flush_task(tid)
        assert first["written"] == 2          # 任务级 + 页面级
        assert second["written"] == 2         # 删旧写新，仍是 2 条
        async with SessionLocal() as session:
            count = len(
                (await session.scalars(
                    select(KnowledgeNode.id).where(KnowledgeNode.task_id == tid)
                )).all()
            )
        assert count == 2
    finally:
        await _cleanup(tid)


@needs_db
async def test_flush_dedups_same_content_across_tasks(fake_env):
    """两个任务内容全同（同一道题跑两轮）→ 只留一份，溯源指向先入库的那个。"""
    t1, t2 = "kn-test-dup1", "kn-test-dup2"
    await _make_task(t1, "重复内容题")
    await _make_task(t2, "重复内容题")
    _write_trace(fake_env, t1, [])   # 不写 trace → 只产生任务级节点，两条内容完全相同
    _write_trace(fake_env, t2, [])
    try:
        r1 = await service.flush_task(t1)
        r2 = await service.flush_task(t2)
        assert r1["written"] == 1
        assert r2["written"] == 0 and r2["reason"] == "duplicate"
        async with SessionLocal() as session:
            rows = (await session.scalars(
                select(KnowledgeNode).where(KnowledgeNode.task_id.in_([t1, t2]))
            )).all()
        assert len(rows) == 1 and rows[0].task_id == t1
    finally:
        await _cleanup(t1, t2)


@needs_db
async def test_flush_missing_task(fake_env):
    result = await service.flush_task("kn-test-nonexistent")
    assert result == {"written": 0, "reason": "task_not_found"}


# ---------------------------------------------------------------- 检索层（N3）


def _vec(axis: int) -> list[float]:
    """构造一个基向量：[0]*512，第 axis 位为 1。两个不同 axis 的余弦相似度=0。"""
    v = [0.0] * 512
    v[axis] = 1.0
    return v


async def _insert_node(
    task_id: str,
    content: str,
    vector: list[float] | None,
    source_type: str = service.SOURCE_TASK,
) -> None:
    """直接插节点（绕过 flush_task）：检索测试要精确控制 embedding。"""
    async with SessionLocal() as session:
        session.add(
            KnowledgeNode(
                source_type=source_type,
                task_id=task_id,
                url="http://x/",
                title=content[:20],
                content=content,
                content_hash=service._content_hash(content),
                embedding=vector,
                meta={},
            )
        )
        await session.commit()


async def test_search_empty_query_returns_empty():
    """空/空白查询直接返回空，不打数据库、不加载模型（纯逻辑，无需 DB）。"""
    assert await service.search("") == []
    assert await service.search("   ") == []


@needs_db
async def test_search_orders_by_similarity(fake_env, monkeypatch):
    """相似度排序：和查询同向的节点必须排第一，且相似度≈1。"""
    t = "kn-test-search"
    await _make_task(t, "检索排序测试")
    try:
        await _insert_node(t, "检索测试-A-同向内容", _vec(0))
        await _insert_node(t, "检索测试-B-正交内容", _vec(1))

        # 桩掉查询编码，让"查询向量"与 A 完全同向
        async def fake_embed(_q: str) -> list[float]:
            return _vec(0)

        monkeypatch.setattr(service, "embed_query", fake_embed)

        # top_k 必须大到能覆盖全库：库里已有上百条真实节点，
        # 正交节点相似度≈0 必然排在最后，取不全的话它根本不在结果里，
        # 后面 next() 就抛 StopIteration（在协程里会包装成很难读的 RuntimeError）
        results = await service.search("任意查询", top_k=1000)
        assert results[0]["content"] == "检索测试-A-同向内容"
        assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-3)
        # 正交那条相似度应为 0（排在后面）
        b = next(r for r in results if r["content"] == "检索测试-B-正交内容")
        assert b["similarity"] == pytest.approx(0.0, abs=1e-3)
    finally:
        await _cleanup(t)


@needs_db
async def test_search_filters_source_type(fake_env, monkeypatch):
    """source_type 过滤：只要页面级时，结果里不能混进任务级。"""
    t = "kn-test-filter"
    await _make_task(t, "来源过滤测试")
    try:
        await _insert_node(t, "过滤测试-任务级", _vec(0))
        await _insert_node(t, "过滤测试-页面级", _vec(0), source_type=service.SOURCE_PAGE)

        async def fake_embed(_q: str) -> list[float]:
            return _vec(0)

        monkeypatch.setattr(service, "embed_query", fake_embed)

        pages = await service.search("查询", top_k=100, source_type=service.SOURCE_PAGE)
        assert pages and all(r["source_type"] == service.SOURCE_PAGE for r in pages)
        assert any(r["content"] == "过滤测试-页面级" for r in pages)
        assert all(r["content"] != "过滤测试-任务级" for r in pages)
    finally:
        await _cleanup(t)


@needs_db
async def test_search_skips_null_embedding(fake_env, monkeypatch):
    """没有向量的节点（SQLite 退化路径/手工插入的脏数据）不能出现在结果里。"""
    t = "kn-test-nullvec"
    await _make_task(t, "空向量测试")
    try:
        await _insert_node(t, "空向量-无 embedding", None)

        async def fake_embed(_q: str) -> list[float]:
            return _vec(0)

        monkeypatch.setattr(service, "embed_query", fake_embed)

        results = await service.search("查询", top_k=100)
        assert all(r["content"] != "空向量-无 embedding" for r in results)
    finally:
        await _cleanup(t)


# ---------------------------------------------------------------- 问答层（N4）


async def test_ask_empty_question_returns_early():
    from app.knowledge import qa

    result = await qa.ask("   ")
    assert result["answer"] == ""
    assert result["note"] == "问题为空"
    assert result["sources"] == []


async def test_ask_no_hits_does_not_call_llm(monkeypatch):
    """检索为空时必须直接拒答，绝不能把"没查到"交给模型编。

    这条是幻觉防线：模型面对"没有材料"时会用训练数据填一个看起来合理的答案。
    """
    from app.knowledge import qa

    async def empty_search(*_a, **_k):
        return []

    class _Boom:
        async def chat(self, *_a, **_k):  # 被调用就说明防线失守
            raise AssertionError("检索为空时不应该调用 LLM")

    monkeypatch.setattr(qa, "search", empty_search)
    monkeypatch.setattr(qa, "get_llm", lambda: _Boom())

    result = await qa.ask("火星上有什么生物？")
    assert result["note"] == "no_hits"
    assert "没有" in result["answer"]


async def test_ask_builds_numbered_material_and_sources(monkeypatch):
    """材料编号 [1][2] 必须和 sources 下标一一对应（前端靠它做引用跳转）。"""
    from app.knowledge import qa

    hits = [
        {
            "task_id": "t1",
            "source_type": service.SOURCE_PAGE,
            "title": "页面一",
            "url": "http://x/1",
            "content": "第一条内容",
            "similarity": 0.9,
            "score": 0.9,
            "meta": {},
        },
        {
            "task_id": "t2",
            "source_type": service.SOURCE_TASK,
            "title": "页面二",
            "url": "http://x/2",
            "content": "第二条内容",
            "similarity": 0.8,
            "score": 0.68,
            "meta": {},
        },
    ]
    captured: dict = {}

    class _FakeLLM:
        async def chat(self, system: str, user: str, **_k) -> str:
            captured["system"] = system
            captured["user"] = user
            return "答案 [1]"

    async def fake_search(*_a, **_k):
        return hits

    monkeypatch.setattr(qa, "search", fake_search)
    monkeypatch.setattr(qa, "get_llm", lambda: _FakeLLM())

    result = await qa.ask("问题", top_k=2)
    # 材料编号在 prompt 里
    assert "[1]" in captured["user"] and "[2]" in captured["user"]
    assert "第一条内容" in captured["user"] and "第二条内容" in captured["user"]
    # system prompt 必须带"不许编造"和"标注来源"两条硬规则
    assert "只能依据给定材料" in captured["system"]
    assert "绝不编造" in captured["system"]
    # sources 下标从 1 开始，与 [n] 对应
    assert [s["index"] for s in result["sources"]] == [1, 2]
    assert result["sources"][0]["task_id"] == "t1"
    assert result["answer"] == "答案 [1]"
