"""语音接口：听（转写）和说（播报）两个入口。

为什么两个接口分别挂在不同的路径前缀下：
- /voice/transcribe 是**无状态工具接口**：给一段音频，返回文字，不涉及任何任务
- /tasks/{id}/speech 是**有状态资源接口**：针对某个已存在任务的结论生成音频
把它们放同一个文件里是因为都依赖语音模块，但别把 transcribe 塞进 /tasks 下面——
转写发生在"还没有任务"的时候（用户正要口述目标），逻辑上不属于任务资源。

设计上的两个刻意选择（和 tasks.py 一致）：
1. 白名单/权限校验放在最外层，不过早下沉，否则错误要等半天才冒出来
2. 失败要能区分"用户问题"（400/413）和"我们这边问题"（503），前端才能给出正确提示
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth.deps import assert_owned, get_current_user
from app.config import settings
from app.db.models import User
from app.runner import task_runner
from app.storage import files
from app.status import UNFINISHED
from app.voice import asr, tts

router = APIRouter(prefix="/api/v1", tags=["voice"])


# 浏览器 MediaRecorder 在不同浏览器产出的 MIME 不一样：
# Chrome/Edge 给 "audio/webm;codecs=opus"，Safari 给 "audio/mp4"，
# Firefox 给 "audio/ogg;codecs=opus"。
# 所以不能精确比对，只看主类型——反正真正的格式判定交给 av 解码器，
# 它比我们猜得准（解不开会抛异常，我们转成 400）。
_ALLOWED_PREFIXES = ("audio/", "video/webm", "application/octet-stream")


def _assert_voice_enabled() -> None:
    """统一开关检查。关掉语音时给 503 而不是 404：
    404 会让前端以为接口写错了，503 才是"这项功能当前不可用"的正确语义。"""
    if not settings.voice_enabled:
        raise HTTPException(status_code=503, detail="语音功能已关闭（VOICE_ENABLED=false）")


@router.post("/voice/transcribe", summary="上传一段录音，返回识别文本")
async def transcribe(
    file: UploadFile = File(..., description="录音文件（webm/wav/mp3/m4a）"),
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> dict:
    """核心链路：收字节 → 丢线程池转写 → 返回文本。

    为什么要用 to_thread：asr.transcribe 是纯 CPU 同步函数，
    直接在 async 函数里调用会把整个事件循环卡住 1~10 秒，
    期间所有接口（包括前端每 1 秒一次的任务轮询）全部停摆。

    M4 收尾补的认证：这个接口原来没有 get_current_user，谁都能白嫖
    ASR 算力（模型占 464MB 内存、单次推理吃满几个核）。
    只做"要登录"，不做归属校验——转写不涉及任何具体任务，没有"归属"可言。
    """
    _assert_voice_enabled()

    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith(_ALLOWED_PREFIXES):
        raise HTTPException(status_code=400, detail=f"不支持的音频类型：{content_type}")

    data = await file.read()

    # 体积上限：spool 到磁盘的大文件如果不拦，能被 500MB 的上传拖死 worker
    limit = settings.voice_max_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"音频超过 {settings.voice_max_mb}MB 上限（实际 {len(data) / 1024 / 1024:.1f}MB）",
        )
    # 空文件/极短文件：多半是用户点了麦克风立刻又点停，直接说没听到，别去跑模型
    if len(data) < 1024:
        raise HTTPException(status_code=400, detail="录音太短，没有识别到内容")

    try:
        result = await asyncio.to_thread(asr.transcribe, data)
    except HTTPException:
        raise
    except Exception as exc:
        # 模型加载失败（网络/磁盘）或解码失败都会走到这里。
        # 不把原始 traceback 抛给前端，但仍把类型和消息带上，方便对着服务端日志排查。
        raise HTTPException(status_code=503, detail=f"语音识别不可用：{type(exc).__name__}: {exc}") from exc

    if not result["text"]:
        # 识别为空是正常业务情况（说得太轻/环境太吵），不是错误。
        # 返回 200 + 空文本，让前端提示"没听清，再说一次"，而不是弹一个红色报错。
        return {"text": "", "duration_s": result["duration_s"], "note": "没有识别到内容，请再说一次"}

    return result


# 失败态 -> 要念出来的话。
# 注意这里是**后端自己的一份**中文映射，前端 types.ts 里还有一份用于显示。
# 两份重复看着不优雅，但职责不同：前端那份是 UI 文案（可以带颜色、可以改版），
# 这份是口语（要顺嘴、要考虑"念出来能不能听懂"），没必要强行共用。
_STATUS_SPEECH = {
    "step_limit": "任务没有完成，步数用完了。",
    "timeout": "任务超时了，没能做完。",
    "loop": "任务卡住了，在原地打转，我主动停了下来。",
    "invalid_action": "任务失败，我连续给出了不合法的操作。",
    "bad_request": "任务参数不对，没能开始。",
    "bad_url": "这个网址不在允许访问的范围内。",
    "nav_failed": "页面打不开，任务没法继续。",
    "error": "任务出错了。",
    # P6b：语音会话里遇到审批停等要念这一句。
    # 为什么必须念："高风险动作等人批准"是安全设计，但语音用户看不到屏幕上的
    # 审批卡片；不告诉他，他只会觉得"说完就没动静了"
    "waiting_approval": "这个操作需要人工批准，请在网页的任务页里批准后再继续。",
}


def _speech_text(meta: dict) -> str:
    """决定念什么。

    这里回答一个产品问题：语音该念"结果"还是念"状态"？
    答案是——**有结果就念结果，没有结果才念状态**。
    因为任务成功时说一句"已完成"对用户毫无信息量（他本来就知道点完了），
    他要听的是答案本身；而任务失败时压根没有答案，只能念原因。

    为什么**不**把 meta.note 拼进来（实测踩到的坑）：
    note 是给开发者和界面看的诊断信息，长这样——
        「连续 3 次相同动作：click e1」
    念出来是"连续三次相同动作，click e一"。英文动作名、元素编号对耳朵毫无意义，
    而且这句话在界面上本来就以文字显示着，念一遍纯属噪音。
    结论：语音只负责"让人知道成没成"，细节留给眼睛。
    """
    answer = (meta.get("answer") or "").strip()
    if answer:
        return answer

    status = meta.get("status") or ""
    return _STATUS_SPEECH.get(status, "任务没有给出结果。")


@router.post("/tasks/{task_id}/speech", summary="为任务结论生成语音播报")
async def make_speech(
    task_id: str,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """合成 mp3 并返回可播放地址。

    幂等：同一个任务重复点播放不会重复调微软的服务。
    这里的"缓存"和通用的 TTS 缓存不是一回事——音频内容由 meta 决定，
    而 meta 一旦任务结束就不再变，所以不存在失效问题，
    一个"文件存在就返回"的判断就够，不需要任何失效策略。
    """
    _assert_voice_enabled()

    # 注意要 await：P3-M2 之后 get_task 会查数据库（异步驱动），
    # 同步调用拿到的是协程对象，meta.get(...) 会直接报 AttributeError。
    # M4：别人的任务在这里也是 404——语音念的可是答案内容，更不能跨用户。
    # assert_owned 现在自己就把"任务不存在"报成 404，这里不用再判一次。
    meta = assert_owned(await task_runner.get_task(task_id), user)

    status = meta.get("status")
    # P5：用 UNFINISHED（排队 / 执行中 / **待审批**）而不是硬编码两个状态。
    # 待审批的任务同样没有 answer，不拦的话会去合成一段空文本——
    # 这正是 app/status.py 存在的意义：加状态时不用满项目找硬编码。
    if status in UNFINISHED:
        # 还没结论，念了也没内容。409 让前端知道"不是失败，是时机不对"
        raise HTTPException(status_code=409, detail="任务还在执行，暂时没有可播报的结论")

    out_path = files.speech_path(task_id)
    url = files.speech_url(task_id)
    text = _speech_text(meta)

    if out_path.exists() and out_path.stat().st_size > 0:
        return {"url": url, "text": text, "cached": True}

    ok, err = await tts.synthesize_safe(text, out_path, settings.tts_voice)
    if not ok:
        # 降级：不抛 500，返回 200 + ok=false。
        # 语音挂了不该让前端弹错误——用户可以照常看文字答案，只是没有播放按钮。
        # 这条边界写在 ADR-006 里。
        return {"url": None, "text": text, "cached": False, "ok": False, "note": err}

    return {"url": url, "text": text, "cached": False, "ok": True}
