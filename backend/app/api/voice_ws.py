"""WebSocket 语音会话：一条长连接里说多轮（P6b 的核心）。

它在项目里的角色：把 P2 的"单轮语音闭环"升级成"持续会话"——
一次连接里可以连着说几轮，转写回显、任务进度、语音播报都在同一条线上，
而且**播报可以被说话打断**（barge-in）。

协议（P6-实施计划.md 第三节，这里实现的就是那张表）：
    C→S  session.start {token}      首帧认证；5 秒内没收到就关连接
    S→C  session.ready {session_id, asr_model}
    C→S  audio.append {seq, data(b64), final}
    S→C  asr.final {text}
    S→C  task.start {task_id, goal}
    S→C  task.step {step, action, ok}
    S→C  task.final {status, answer, task_id}
    S→C  tts.chunk {seq, data(b64 mp3)} / tts.end {seq, interrupted}
    C→S  interrupt {}                打断播报
    S→C  error {code, message}

四个关键决定（每条都对应计划里的一处论证）：

1. **token 走首帧，不走 URL**。URL 会进 uvicorn 访问日志、浏览器历史、代理日志。
   身份凭证不该出现在 URL 里——和 P5b 把媒体地址改成签名 URL 是同一个思路。

2. **一句语音 = 一个任务，不串上下文**。跨轮指代（"它"、"那个"）要维护会话级
   记忆，会和 P4 的知识库打架；P6 的验收线是"实时语音对话可演示"。
   这个边界写进文档：**能一句一句问，不共享上下文**。

3. **任务进度走 Redis pub/sub，Redis 不可用降级轮询**（见 runner/events.py）。
   WS 连在 API 进程，任务跑在 worker 进程，事件桥是唯一能"实时"的那条路。

4. **播报可打断**：reader 任务持续读消息，播报中收到 audio.append/interrupt
   就置 barge 标志，播报循环每个 chunk 检查一次，命中即停并丢弃后续 chunk。
   用户说的话依然留在队列里（下一句照常转写），不能因为是"打断"就把音频扔了。

刻意**不做**的事：
- 不做真·逐字流式 ASR（faster-whisper 是批处理模型，换引擎要动整套推理，
  本阶段做伪流式，见计划第一节约束 2）；
- 不做语音里的审批批准（审批是高风险动作的最后一道闸，必须带画面和上下文
  在网页上做；语音里只念一句"需要你在网页上批准"）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.voice import _speech_text  # 复用手写的中文口语映射，别养两份
from app.auth.service import AuthError, verify_token
from app.config import settings
from app.runner import events, task_runner
from app.voice import asr, tts

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

# 首帧认证的超时：连上以后 5 秒内必须自报身份，否则关连接。
# 不设的话，一个只连不说话的连接能一直占着资源（最简单的资源耗尽手法）
AUTH_TIMEOUT = 5.0

# 一句话最少多少字节。太小多半是"点了麦克风立刻又点停"，
# 送去 ASR 只会得到空文本，白跑一次模型（464MB 模型单次推理吃满几个核）
MIN_AUDIO_BYTES = 1024

# 一句话最多多少字节（沿用 REST 语音接口的上限配置）
# 超过就丢弃这轮音频：语音是"说一句"，不是"上传录音文件"
def _max_audio_bytes() -> int:
    return settings.voice_max_mb * 1024 * 1024


def _start_url() -> str:
    """语音任务从哪一页开始。

    P6 范围内固定在本地测试站首页：语音里没有"网址"这个概念
    （模型也不该从口语里猜 URL），先把"说一句 → 跑一趟"跑通。
    将来要支持任意站点，正确做法是让前端在 session.start 里带一个
    start_url（并走白名单校验），而不是让 ASR 文本去猜。

    DEFAULT_START_URL 给容器模式用：start_url 最终是 worker 容器里的
    Chromium 去导航的，它的 127.0.0.1 不是 api —— compose 里必须配成
    http://api:8000/testsite/index.html（2026-10-28 check_voice_ws 实测踩过）。
    """
    if settings.default_start_url:
        return settings.default_start_url
    return f"http://127.0.0.1:{settings.app_port}/testsite/index.html"


async def _send(ws: WebSocket, payload: dict) -> bool:
    """发一条消息；连接已断返回 False（调用方据此收尾）。"""
    try:
        await ws.send_json(payload)
        return True
    except Exception:  # noqa: BLE001 —— 断开是常态，不是异常
        return False


async def _error(ws: WebSocket, code: str, message: str) -> bool:
    return await _send(ws, {"type": "error", "code": code, "message": message})


@router.websocket("/api/v1/voice/ws")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()

    # ---- 0. 功能开关：关掉时明确告知并关闭（和 REST 的 503 同一语义）----
    if not settings.voice_enabled:
        await _error(ws, "voice_disabled", "语音功能已关闭（VOICE_ENABLED=false）")
        await ws.close(code=4004)
        return

    session_id = uuid.uuid4().hex[:12]

    # ---- 1. 首帧认证 ----
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        await _error(ws, "auth_timeout", f"{AUTH_TIMEOUT:.0f} 秒内没有收到 session.start")
        await ws.close(code=4001)
        return
    except (WebSocketDisconnect, RuntimeError):
        return
    
    if not isinstance(first, dict) or first.get("type") != "session.start":
        await _error(ws, "bad_handshake", "首帧必须是 session.start")
        await ws.close(code=4001)
        return

    user_id: int | None = None
    if settings.auth_enabled:
        try:
            user_id = verify_token(str(first.get("token") or ""))
        except AuthError as exc:
            # 认证失败给明确的原因（"过期"/"格式不对"），但不区分伪造和过期——
            # verify_token 已经统一了口径，这里原样转发即可
            await _error(ws, "unauthorized", str(exc))
            await ws.close(code=4003)
            return

    if not await _send(
        ws,
        {"type": "session.ready", "session_id": session_id, "asr_model": settings.asr_model},
    ):
        return

    # ---- 2. 会话主循环 ----
    # 为什么要一个独立的 reader 任务：主循环在跑 ASR / 等任务 / 播报时
    # **读不动 socket**，而打断恰恰发生在"播报中"。用队列把读和做解耦，
    # barge 标志专门负责"播报中有人说话"这一件事。
    queue: asyncio.Queue[dict] = asyncio.Queue()
    barge = asyncio.Event()
    speaking = {"on": False}

    async def reader() -> None:
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    mtype = msg.get("type")
                    # 播报中收到新的语音块或显式 interrupt → 打断
                    if mtype == "interrupt" or (mtype == "audio.append" and speaking["on"]):
                        barge.set()
                await queue.put(msg if isinstance(msg, dict) else {})
        except WebSocketDisconnect:
            await queue.put({"type": "__closed__"})
        except Exception:  # noqa: BLE001 —— 读失败一律当断开处理
            await queue.put({"type": "__closed__"})

    reader_task = asyncio.create_task(reader())
    audio_buf: list[bytes] = []

    try:
        while True:
            msg = await queue.get()
            mtype = msg.get("type")

            if mtype == "__closed__":
                break
            if mtype == "session.end":
                await _send(ws, {"type": "session.ended", "session_id": session_id})
                break
            if mtype == "ping":
                await _send(ws, {"type": "pong"})
                continue
            if mtype == "interrupt":
                # 空闲时的 interrupt 无事可做（播报中的那个已经由 barge 处理）
                continue

            if mtype == "audio.append":
                try:
                    raw = base64.b64decode(msg.get("data") or "", validate=True)
                except (binascii.Error, ValueError):
                    await _error(ws, "bad_audio", "音频块不是合法的 base64")
                    continue
                if raw:
                    audio_buf.append(raw)
                if sum(len(b) for b in audio_buf) > _max_audio_bytes():
                    audio_buf.clear()
                    await _error(
                        ws, "audio_too_large", f"这一句超过 {settings.voice_max_mb}MB 上限"
                    )
                    continue
                if msg.get("final"):
                    data = b"".join(audio_buf)
                    audio_buf.clear()
                    if not await _handle_sentence(ws, data, user_id, queue, barge, speaking):
                        break
                continue

            await _error(ws, "bad_message", f"未知消息类型：{mtype}")
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def _handle_sentence(
    ws: WebSocket,
    data: bytes,
    user_id: int | None,
    queue: asyncio.Queue,
    barge: asyncio.Event,
    speaking: dict,
) -> bool:
    """一整句：转写 → 提交任务 → 转发进度 → 播报结论。返回 False 表示连接断了。"""
    if len(data) < MIN_AUDIO_BYTES:
        return await _error(ws, "too_short", "录音太短，没有识别到内容，请再说一次")

    # ---- 转写（ASR 是同步 CPU 活，必须丢线程池，否则整条事件循环停摆）----
    try:
        result = await asyncio.to_thread(asr.transcribe, data)
    except Exception as exc:  # noqa: BLE001
        return await _error(ws, "asr_failed", f"语音识别不可用：{type(exc).__name__}: {exc}")

    text = (result.get("text") or "").strip()
    if not await _send(ws, {"type": "asr.final", "text": text}):
        return False
    if not text:
        # 空转写是正常业务情况（说得太轻/太吵），不是错误——
        # 给一句提示让用户重说，而不是把会话搞断
        return await _error(ws, "empty_text", "没听清，请再说一次")

    # ---- 提交任务 ----
    # require_approval 用默认值（True）：语音会话**不能**天然免审批。
    # 高风险动作照样停下来等人，只是"等人"这件事必须在有画面的网页上做，
    # 所以下面遇到 waiting_approval 就念一句"请去网页批准"。
    try:
        task_id = await task_runner.submit(
            goal=text,
            start_url=_start_url(),
            user_id=user_id,
            require_approval=True,
        )
    except Exception as exc:  # noqa: BLE001
        return await _error(ws, "submit_failed", f"任务提交失败：{type(exc).__name__}: {exc}")

    if not await _send(ws, {"type": "task.start", "task_id": task_id, "goal": text}):
        return False

    # ---- 转发事件，直到终态 ----
    final: dict | None = None
    async for event in events.follow(task_id):
        payload = {"type": event["event"], **{k: v for k, v in event.items() if k != "event"}}
        if not await _send(ws, payload):
            return False
        if event.get("event") == events.FINAL_EVENT:
            final = event
            break

    if final is None:
        # events.follow 只在两种情况下不带 final 就结束：连接取消。
        # 走到这里说明是真断了，不再尝试播报
        return True

    # ---- 播报结论：有答案念答案，没答案念状态（和 REST 播报同一套口径）----
    speech = _speech_text(
        {"answer": final.get("answer") or "", "status": final.get("status") or ""}
    )
    await _speak(ws, speech, barge, speaking)
    return True


async def _speak(ws: WebSocket, text: str, barge: asyncio.Event, speaking: dict) -> None:
    """流式播报一段话；期间用户开口就立刻停。

    打断的粒度是"每个音频块检查一次"而不是"实时取消"：
    edge-tts 的块间隔在几十毫秒级，人耳感知不出差别，
    而真正的取消要处理生成器清理，收益不抵复杂度（计划里 barge-in 的
    "低成本"判断指的就是这个）。
    """
    speaking["on"] = True
    barge.clear()
    seq = 0
    interrupted = False
    try:
        # 会话播报限 300 字：口语里超过半分钟的内容没人会听完，
        # 而且用户在等下一轮，长播报只会让人想打断
        async for chunk in tts.synthesize_stream(text, settings.tts_voice, max_chars=300):
            if barge.is_set():
                interrupted = True
                break
            if not await _send(
                ws,
                {"type": "tts.chunk", "seq": seq, "data": base64.b64encode(chunk).decode("ascii")},
            ):
                return
            seq += 1
    except Exception as exc:  # noqa: BLE001 —— 播报失败只提示，不结束会话
        await _error(ws, "tts_failed", f"语音合成失败：{type(exc).__name__}: {exc}")
    finally:
        speaking["on"] = False
    await _send(ws, {"type": "tts.end", "seq": seq, "interrupted": interrupted})
