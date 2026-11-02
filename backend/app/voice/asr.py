"""语音转文字（ASR）：把用户录的一段话变成"任务目标"文本。

它在项目里的角色：
- 前端麦克风录出 webm/opus → 上传到 /api/v1/voice/transcribe → 这里转成中文
- 语音只是**输入的另一种形式**，转完就退场：Agent 干活走的还是纯文本那条路
  换句话说，ASR 是"入口加宽"，不是"能力升级"——它不提高任务成功率

为什么用 faster-whisper 而不是调云端 ASR：
- 云端 ASR（讯飞/智谱）都要额外 key，章程限定只用免费档
- faster-whisper 是 whisper 的 CTranslate2 实现，CPU + int8 就能跑
  实测 small 模型：加载约 10s（一次性），5.7 秒音频转写约 1.5s
- 代价是模型 464MB 存在本地缓存里，所以下面必须做「单例 + 预热」

三个性能/正确性上的关键决定，都写在对应函数里：
1. _load_model()：双检锁单例，绝不每次请求加载（否则每次 +10s）
2. transcribe()：丢到工作线程里跑，不能直接在事件循环里转写（会卡死整个服务）
3. transcribe()：全局串行，CPU int8 转写会吃满核，并发只会互相拖慢
"""
from __future__ import annotations

import io
import os
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# ⚠️ 顺序很重要，这三步不能换位置
# 第一步：先 import config —— 它会 load_dotenv()，把 .env 里的值读进环境变量
from app.config import settings

# 第二步：再补默认值。用 setdefault 而不是直接赋值：
# 用户在 .env 里显式写过 HF_ENDPOINT，就该用他的（比如他配了公司内网镜像）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 这一行是踩坑换来的，不能删：
# hf-mirror 只代理了模型索引和普通文件，没有代理 HuggingFace 的 Xet 存储后端。
# 不关掉 Xet，下载会中途跳到 cas-server.xethub.hf.co，直接 401 Unauthorized。
# 现象很像"镜像挂了"，实际是镜像不覆盖那块，必须显式回退普通 HTTP 下载。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Windows 默认不支持符号链接，huggingface_hub 每次加载都会打一大段警告，纯噪音
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# 第三步：faster_whisper 才在 _load_model() 里 import（见下方的懒加载理由）


# 模型单例。进程内只有一份，所有请求共用。
_model: Any = None

# 一把锁同时干两件事，这是刻意的简化：
#   1) 防并发重复加载模型（两个请求同时进来，只有一个真去下/加载）
#   2) 串行化转写本身（CPU int8 会把核吃满，两个并发转写总耗时更长，还会拖慢 API）
#
# 为什么是 RLock 而不是 Lock——这是个踩过的坑：
# transcribe() 会先拿锁，然后在锁内部调 _load_model()，而 _load_model() 自己也要拿锁。
# 普通 Lock 不可重入，同一线程第二次 acquire 会**永久阻塞**（自己等自己）。
# 症状极其难查：请求一发出去就没反应，没有报错、没有日志，端口还在监听。
# RLock（可重入锁）记录了"持锁的是哪个线程"，同线程再进直接放行。
#
# 为什么不用 asyncio.Lock：真正干活的是 worker 线程，
# asyncio.Lock 绑在事件循环上，跨线程拿它没有意义。
_lock = threading.RLock()


def _load_model() -> Any:
    """加载 whisper 模型（只执行一次）。

    为什么不用模块级 `from faster_whisper import WhisperModel`：
    import faster_whisper 会连带加载 ctranslate2 的 native 库，启动慢好几百毫秒。
    如果 VOICE_ENABLED=false（比如做纯文本评测），这份开销纯属浪费。
    """
    global _model
    if _model is not None:          # 快路径：绝大多数请求走到这里就返回
        return _model
    with _lock:                     # 慢路径：只有加载时才抢锁
        if _model is None:          # 双检：抢到锁后再看一次，避免排队进来重复加载
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                settings.asr_model,
                device="cpu",           # 没有独显也能跑；有 CUDA 时这里可以换 "cuda"
                compute_type="int8",    # 8bit 量化：体积小、速度快，中文识别几乎不掉点
            )
    return _model


def preload_import() -> None:
    """在主线程里把重模块 import 一遍（不加载模型）。

    ⚠️ 为什么需要这个（真实踩到的并发 bug）：
    模型加载放在后台线程里跑，而线程里第一次 `import faster_whisper` 会连带
    import `huggingface_hub` → `httpx2`。如果这时候第一个任务请求也在 import
    同一批模块（OpenAI SDK 也用 httpx2），两个线程会同时初始化同一个包，
    CPython 就抛：

        AttributeError: partially initialized module 'httpx2' has no attribute 'URL'
        (most likely due to a circular import)

    这个错只在"服务刚起 + 立刻有任务"时偶发，日志看起来像依赖坏了，
    实际是 import 竞态。解法不是加锁，而是**让 import 只在一个线程里发生**：
    lifespan 里先同步调这个函数（约 1 秒），再开后台线程去加载模型。

    为什么必须三行都写（少一行就等于没预载）——依赖链是分层的：
        faster_whisper            ← 外壳，不含下载逻辑
        faster_whisper.utils      ← :7 `import huggingface_hub`，下载入口在这里
        huggingface_hub.file_download / hf_api  ← :16 `import httpx2`
    只 import 第一层时，后两层仍然是后台线程第一次触发的。
    所以末行直接 import httpx2：让它在主线程**完整执行完模块体**，
    sys.modules 里存的就是成品；之后任何线程拿到的都不可能是半成品。
    """
    import faster_whisper        # noqa: F401
    import faster_whisper.utils  # noqa: F401  # 真正的下载路径
    import httpx2                # noqa: F401  # 竞态的受害者，压平它


def warmup() -> None:
    """预热：启动时先把模型加载进内存。

    不预热的话，用户第一次说话要先等 10 秒加载，体验像"卡住了"。
    这个函数是同步阻塞的，调用方必须丢到线程里（见 main.py 的 lifespan）。
    """
    _load_model()


def transcribe(data: bytes) -> dict:
    """把一段音频字节转成文字。**这是同步函数，调用方要用 to_thread 包一层。**

    为什么不写成 async def：
    转写是纯 CPU 计算，中间没有任何 await 点。写成 async def 也只是把阻塞代码
    塞进事件循环——整个服务（包括正在跑的任务轮询）会被卡住 1~10 秒。
    正确做法是 asyncio.to_thread(transcribe, data)，让它去 worker 线程里跑。

    参数
    ----
    data: 音频原始字节。webm/opus、wav、mp3、m4a 都行——
          解码交给 av（PyAV，自带 ffmpeg），不需要外部 ffmpeg 命令。

    返回
    ----
    {"text": 识别文本, "duration_s": 音频时长, "elapsed_s": 本次耗时}
    识别不出内容时 text 是空字符串，不抛异常——"没听清"是正常业务情况，
    由上层决定要不要提示用户重录。
    """
    if not data:
        return {"text": "", "duration_s": 0.0, "elapsed_s": 0.0}

    started = time.perf_counter()

    with _lock:                     # 串行化：见文件头第 3 条
        # 注意：下面这行会在持锁状态下再进 _load_model 的 with _lock。
        # 因为 _lock 是 RLock，同线程重入直接放行；换成 Lock 这里就死锁了。
        model = _load_model()
        segments, info = model.transcribe(
            # BytesIO：把上传的字节当成文件对象喂给解码器。
            # 不落临时文件，省掉一次磁盘写和一处要清理的垃圾。
            io.BytesIO(data),
            language=settings.asr_language,
            # beam_size=1 是贪心解码。语音指令是短句、不需要"最优解"，
            # 换取将近一倍的提速（实测 1.5s vs 3s+）。
            beam_size=1,
            # VAD：先用静音检测切掉前后空白和长停顿。
            # 用户按下按钮后往往先愣半秒、说完再愣一秒，不切的话
            # whisper 会在静音段上"脑补"出不存在的句子（幻听）。
            vad_filter=True,
        )
        # segments 是生成器，必须遍历完才有结果——这一步才是真正的计算
        text = "".join(seg.text for seg in segments).strip()

    return {
        "text": text,
        "duration_s": round(float(getattr(info, "duration", 0.0)), 2),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }
