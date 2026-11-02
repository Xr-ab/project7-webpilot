"""文本向量化：bge-small-zh-v1.5 本地推理。

它在项目里的角色：P4 的"翻译官"——把中文文本翻译成 512 维向量，
让"语义相近"变成"距离相近"，检索才能按意思找而不是按字面匹配。

为什么用本地模型而不是 embedding API：
- 免费、离线、无额度焦虑。回填 36 个任务就是 100+ 次编码，调 API 既慢又耗额度
- p5 用过同款 bge-small-zh，中文短文本效果好；512 维比 1024/1536 维省一半存储
- 代价：首次加载约 1~2 秒 + 模型 95MB（torch 运行时内存占大头）

为什么懒加载 + 单例：
- 模型加载要 1~2 秒，API 进程启动时不该付这笔钱（只有真正检索时才用得上）
- 单例避免重复 load —— sentence-transformers 每次 new 都会吃一份内存，重复加载会 OOM
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# bge-small-zh-v1.5：中文小模型，512 维。换模型必须同步改 models.EMBED_DIM 并重嵌全表
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

# bge 官方建议的检索指令前缀：短查询去检索长文档时加上，效果更稳。
# v1.5 版本说"可加可不加"，但加上不亏——代价只是查询向量多编码几个字。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class Embedder:
    """一个模型实例 + 批量编码接口。

    线程安全：sentence-transformers 的 encode 是纯推理，多线程并发调用是安全的
    （底层 torch 自带调度），所以这里不加锁——加锁反而把并发编码串行化了。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None  # 真正加载发生在第一次 encode（懒加载）
        self._dim: int | None = None

    def _ensure_model(self):
        """第一次用到时才 import + load。

        为什么 import 也放函数内：sentence_transformers 会连带 import torch，
        这一步就是几百毫秒——API 进程启动、跑单测都不该为它买单。
        """
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("加载 embedding 模型 %s（首次约 1~2s）", self.model_name)
            self._model = SentenceTransformer(self.model_name, device=self.device)
            # sentence-transformers 6.x 把 get_sentence_embedding_dimension 改名成
            # get_embedding_dimension；老版本仍是旧名。两个都试，避免升级时踩空。
            getter = getattr(self._model, "get_embedding_dimension", None) or getattr(
                self._model, "get_sentence_embedding_dimension"
            )
            self._dim = getter()
            log.info("模型就绪，维度 %s", self._dim)
        return self._model

    @property
    def dim(self) -> int:
        self._ensure_model()
        return self._dim or 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        """批量编码。normalize_embeddings=True 是关键：

        向量检索用的是余弦相似度。归一化后余弦 = 内积，数据库侧就能用
        `<=>`（cosine distance）算子走索引；不归一化会让"长文本向量模长大"
        干扰相似度排序，检索结果看起来"莫名其妙地偏"。
        """
        if not texts:
            return []
        model = self._ensure_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            # 转成普通 list：SQLAlchemy 的 Vector 列写入认 list，
            # 传 numpy array 在某些驱动下会报 "unsupported type"
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        """异步包装：编码是 CPU 密集的同步调用，直接在事件循环里跑会阻塞整个 API。

        to_thread 把它丢进线程池——回填/检索期间其它请求照常响应。
        这是所有"同步库往 async 项目里塞"的通用解法。
        """
        if not texts:
            return []
        return await asyncio.to_thread(self.encode, texts)


# 模块级单例：整个进程一份模型
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


async def embed_query(text: str) -> list[float]:
    """查询侧编码：加指令前缀（文档侧不加——bge 的非对称检索约定）。"""
    vectors = await get_embedder().aencode([QUERY_INSTRUCTION + text])
    return vectors[0]
