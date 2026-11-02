"""GLM 客户端封装：全项目唯一跟模型服务通信的地方。

为什么用 AsyncOpenAI 而不是 OpenAI：
- 循环跑在 asyncio 事件循环里。同步客户端一调就会把整个事件循环堵住
  （M2 讲过的"async 里塞同步阻塞代码"就是这个坑，换成网络请求一样会堵）。
- 异步客户端 await 期间事件循环能去干别的，将来并发跑多个任务时才站得住。

为什么要自己写"容错解析"：
- 模型很爱把 JSON 包在 ```json 里，或者在前后加一句"好的，我来点击"。
- 这不是模型错，是它的默认习惯。硬解析必炸，所以先剥壳再取第一个 { 到最后一个 }。
"""
from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


class LLMFormatError(RuntimeError):
    """模型输出不是合法 JSON。上层可以重试一次，或者判失败。"""


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象。

    处理三种常见脏输出：
    1. ```json {...} ``` 代码块
    2. 前后夹着解释文字
    3. 结尾多一个逗号之类的轻微格式问题（这个交给 json 报错，不硬修）
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise LLMFormatError(f"输出里找不到 JSON 对象：{raw[:120]!r}")

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMFormatError(f"JSON 解析失败（{exc.msg}）：{raw[:120]!r}") from exc

    if not isinstance(data, dict):
        raise LLMFormatError("JSON 顶层必须是对象，不是数组或字符串")
    return data


class LLMClient:
    """一个进程共用一份即可（内部用连接池），不要每个任务建一个。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.model = model or settings.llm_model
        self._client = AsyncOpenAI(
            api_key=api_key or settings.llm_api_key,
            base_url=base_url or settings.llm_base_url,
            timeout=timeout,
            max_retries=1,  # 网络抖动自动重试；解析失败由 decide 自己重试
        )
        # 最近一次调用的 token 用量，写进 trace 便于算成本
        self.last_usage: dict[str, Any] = {}

    async def chat(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        """一轮对话，返回纯文本。temperature=0 让同样的输入尽量得到同样的输出——排查问题时这点很重要。"""
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        if resp.usage is not None:
            self.last_usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return resp.choices[0].message.content or ""

    async def decide(self, system: str, user: str) -> dict[str, Any]:
        """要一个动作。格式不对就带着"错在哪"重试一次，还不成就抛出去让循环记失败。"""
        reply = await self.chat(system, user)
        try:
            return extract_json(reply)
        except LLMFormatError as first_error:
            retry_user = f"{user}\n\n[注意] 上一次输出不是合法 JSON（{first_error}）。只输出一个 JSON 对象。"
            reply = await self.chat(system, retry_user)
            return extract_json(reply)
