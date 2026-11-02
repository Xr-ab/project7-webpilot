"""视觉兜底：DOM 快照看不见的东西，让视觉模型"看一眼"截图。

角色（P6 决策 2-A）：**只做"理解"，不做"动作"**。
- 输入：一张 PNG 截图 + 一句问话
- 输出：一段中文描述，或者 None（失败降级）
- 绝不做：返回坐标、生成点击、改动 action。动作永远走 DOM 编号那条路
  （点错坐标没有任何校验能拦住，而 e3 一定有 [data-wp-id] 可以验）。

为什么失败返回 None 而不是抛异常（和 llm.py 的 decide 相反）：
- decide 失败 = 任务没法继续，必须炸出来让人看见；
- 视觉失败 = 少看一张图而已，DOM 那条路还通着。
  把"增强项故障"升级成"任务失败"是拿错误换正确，
  所以这里吞掉一切异常，只留一行 warning 日志。

为什么退避只有 3s/8s（预检脚本用的是 15/45/90s）：
预检的目标是"把这一次实验跑完"，可以等 90 秒；
工作流里视觉藏在某一步里，等 90 秒等于把整步拖死——
宁可"这次没看到图"，也不能让任务卡在这儿。免费档主力模型 glm-4v-flash
实测几乎不撞 429（2026-10-28 预检），短退避够用。
"""
from __future__ import annotations

import base64
import logging

from openai import AsyncOpenAI

from app.config import settings

log = logging.getLogger(__name__)

# 官方限制：单图 ≤ 5MB（P6 决策 5 的调研）。超限直接放弃，
# 不发出去挨一个 400——既省一次往返，也避免把大图塞进上下文。
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# 重试的等待秒数：短退避，理由见模块说明。总预算 ≈ 11s + 两次请求耗时。
_RETRY_WAITS = (3, 8)

# 默认问法。不给模型开放式指令（"描述这个页面"会得到一堆无关细节），
# 而是明确要三类东西：文字、数字、可见控件——正好是 DOM 快照丢失的那部分。
DEFAULT_QUESTION = (
    "这是浏览器当前页面的截图。请用中文简洁列出："
    "1) 图中能看清的所有文字（按钮、标题、说明）；"
    "2) 出现的数字和它们对应的含义；"
    "3) 图表/图形在表达什么（如果有）。"
    "只描述你真正看到的内容，不要推测页面之外的东西。"
)

# 进程内单例：AsyncOpenAI 内部带连接池，每个任务建一个纯属浪费。
# 懒加载而不是模块级直建：import 本模块时不应要求 LLM_API_KEY 已就绪
#（比如跑不碰视觉的单测）。
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            # 视觉比纯文本慢（图片编码 + 推理），给足 60s；
            # llm.py 的 30s 是给文本对话定的，不能照搬
            timeout=60.0,
            max_retries=0,  # 重试由本模块自己管：要按 429 区分退避策略
        )
    return _client


async def describe(
    image: bytes,
    question: str = DEFAULT_QUESTION,
    *,
    model: str | None = None,
) -> str | None:
    """让视觉模型描述一张截图。失败一律返回 None（不抛）。

    参数：
        image: PNG 字节。必须和 take_snapshot 同一时刻的视口截图，
               否则模型说的"左上角按钮"和 DOM 编号对不上（同一帧是隐含契约）。
        question: 问什么。默认问文字/数字/图形三类。
        model: 覆盖视觉模型（测试用；生产读 settings.vision_model）。

    返回：
        模型的中文描述（已 strip）；任何失败路径都返回 None。
    """
    if not image:
        log.warning("视觉调用跳过：截图字节为空")
        return None
    if len(image) > MAX_IMAGE_BYTES:
        log.warning("视觉调用跳过：截图 %d 字节超过 5MB 上限", len(image))
        return None

    model = model or settings.vision_model
    b64 = base64.b64encode(image).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                # 顺序有讲究：图在前、问题在后。反过来模型容易先按文字
                # 猜一堆、再看图凑答案（实测 GLM 视觉系更稳的顺序）
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": question},
            ],
        }
    ]

    for attempt in range(len(_RETRY_WAITS) + 1):
        try:
            resp = await _get_client().chat.completions.create(
                model=model, messages=messages, temperature=0.0
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or None
        except Exception as exc:  # noqa: BLE001 —— 见模块说明：视觉失败一律降级
            status = getattr(exc, "status_code", None)
            # 429（限流）和 5xx（服务端抖动）值得等一等；
            # 4xx 里其余的错误（401 key 错、400 参数错、404 模型名错）
            # 等多久都不会变好，直接放弃
            retryable = status in (429, 500, 502, 503, 504)
            if retryable and attempt < len(_RETRY_WAITS):
                wait = _RETRY_WAITS[attempt]
                log.warning(
                    "视觉调用失败（HTTP %s，第 %d 次），%ds 后重试：%s",
                    status,
                    attempt + 1,
                    wait,
                    str(exc)[:160],
                )
                # 注意：async 里不能 time.sleep，会堵住整个事件循环
                import asyncio

                await asyncio.sleep(wait)
                continue
            log.warning(
                "视觉调用放弃（HTTP %s，model=%s）：%s", status, model, str(exc)[:160]
            )
            return None
    return None  # 理论上到不了：循环内每轮都 return 了
