"""语音合成（TTS）：把任务结论念出来。

它在项目里的角色：
- 任务跑完之后，前端点「播放」→ 这里把答案文本合成 mp3 → 存进任务目录 → 前端 <audio> 播
- 和 asr.py 对称：ASR 是"入口加宽"，TTS 是"出口加宽"，都不影响 Agent 本身能不能干成活

为什么用 edge-tts：
- 免费、不用 key、中文音色质量接近商用水平（Azure 同一套神经音色）
- 代价 1：它**不是本地模型**，每次合成都要连微软的服务器。
  实测延迟稳定在 1.4~1.7 秒，而且几乎不随文本长度变化——
  说明这 1.5 秒基本是"握手 + 首包"的固定开销，不是编码时间。
- 代价 2：网络不通就直接失败。所以调用方必须把 TTS 失败**降级处理**：
  答案照常显示，只是没有播放按钮。语音是锦上添花，不能因为它把主流程搞挂。

为什么原来是"不做流式（边合成边播）"（P2），后来在 P6 补了 synthesize_stream：
章程里定的是「非流式」——1.5 秒对"任务跑完点一下播放"这个场景完全够用。
P6b 的语音**会话**是另一个场景：一条 WS 长连接里连着说几轮，等 1.5s 再开口
会有明显的"机械感"，所以补了一条**只推块、不落盘**的流式出口。
两者并存是有意的：REST 播放（/tasks/{id}/speech）走文件 + 幂等缓存，
会话播报走内存流 + 可打断，各自的约束完全不同，硬统一只会互相拖累。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

# 单次合成的文本上限。超过这个长度大概要念 3 分钟，用户根本不会听完，
# 而且会让 mp3 文件膨胀到几 MB。截断比"合成半天"更符合实际使用。
MAX_SPEECH_CHARS = 500

# 匹配 markdown 里那点残留记号（答案偶尔会带 ** 或 ` 这类强调符号）。
# 不追求完整解析 markdown——只是让念出来别把星号读成"星号星号"。
_NOISE = re.compile(r"[*_`#>]+")


def _clean(text: str) -> str:
    """把一段答案整理成"适合念出来"的文本。

    为什么不能直接念原始答案：
    - 换行符会被 TTS 读成停顿，但列表式的答案换行特别多，念起来一顿一顿
    - 中英文混排时空格多了会停顿怪异
    所以统一压成一句话式，只保留中文标点作为停顿依据。
    """
    # 换行 → 逗号：列表项之间的停顿应该比句子短，用句号会太碎
    text = re.sub(r"\s*\n+\s*", "，", text)
    text = _NOISE.sub("", text)
    # 多个空格压成一个（中文里空格本来就少）
    text = re.sub(r"[ \t]+", " ", text)
    # 连续标点：逗号叠逗号会让 TTS 停顿过长
    text = re.sub(r"[，,]{2,}", "，", text)
    text = text.strip("， ,")
    return text


async def synthesize(text: str, out_path: Path, voice: str) -> Path:
    """把文本合成为 mp3，写到 out_path，返回路径。

    调用方负责 try/except：这个函数失败时直接抛异常，不吞。
    吞掉异常会让"点了播放没反应"变成无法排查的幽灵 bug。

    voice 形如 "zh-CN-XiaoxiaoNeural"（微软音色名）。
    """
    import edge_tts  # 懒加载：不启用语音时省掉 import 开销

    cleaned = _clean(text)[:MAX_SPEECH_CHARS]
    if not cleaned:
        raise ValueError("没有可朗读的文本")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 先写 .tmp 再 rename，理由和 storage/files._write_json 一样：
    # 前端可能一边在加载音频一边服务在写文件，读到半截 mp3 会直接播不出来。
    # rename 是原子的，读到的要么是完整文件、要么根本没有。
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    communicate = edge_tts.Communicate(cleaned, voice)

    # 不用 communicate.save()：那个内部也是流式写，但我们要自己控制写入位置，
    # 而且 stream() 的返回结构让我们能只挑 audio 类型的 chunk 写盘。
    with open(tmp, "wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])

    # 一个字节都没写出来说明服务端返回了纯文本错误（比如音色名拼错）
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("TTS 返回空音频（检查音色名是否正确）")

    tmp.replace(out_path)
    return out_path


async def synthesize_safe(text: str, out_path: Path, voice: str) -> tuple[bool, str]:
    """synthesize 的降级包装：失败不抛，返回 (是否成功, 错误说明)。

    用途是"播报失败不该影响主流程"。TTS 挂了，任务结果照样要能看。
    """
    try:
        await synthesize(text, out_path, voice)
        return True, ""
    except asyncio.CancelledError:
        raise  # 取消是正常控制流，不能当成业务失败吞掉
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def synthesize_stream(
    text: str,
    voice: str,
    *,
    max_chars: int = MAX_SPEECH_CHARS,
):
    """边合成边吐 mp3 块（P6b-B4）。

    和 synthesize 的关系：同一个 edge-tts 接口，区别是**不写文件**——
    语音会话要把音频块推给浏览器边收边播，落盘只会在磁盘上留一堆
    没人再听的临时文件（用户说完就走的语音不该留痕，和"语音分片不落盘"
    是同一条隐私纪律）。

    为什么这里值得流式：edge-tts 的 1.5s 是"握手 + 首包"的固定开销，
    首包一到就开始播，用户主观感受从"等 1.5s"变成"立刻说话"。

    ⚠️ 调用方负责取消：用户打断时取消这个生成器即可，
    edge-tts 会随之停掉上游连接（不要在调用方自己 break 后还留着任务）。
    """
    import edge_tts  # 懒加载：不启用语音时省掉 import 开销

    cleaned = _clean(text)[:max_chars]
    if not cleaned:
        return

    communicate = edge_tts.Communicate(cleaned, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]
