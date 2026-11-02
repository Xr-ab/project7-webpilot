"""语音会话 WS 端点的测试（P6b-B1/B2/B4）。

测试策略：只起一个**最小 FastAPI app**（只挂 voice_ws 路由），不触发 main.py 的
lifespan —— 那个会加载 464MB 的 ASR 模型，把 0.1 秒的测试变成 10 秒，断网还会随机红。

ASR / 任务 / TTS 全部 monkeypatch：
- ASR 真跑要加载模型，单测不碰；
- 任务真跑要浏览器 + worker，那是 check_voice_ws.py 的活；
- TTS 真跑要连微软，而且这里要验证的是"块有没有推给前端、打断有没有生效"。

覆盖的验收线（P6 计划 B1/B2/B4）：
1. 首帧不是 session.start → 拒绝（4001）
2. 认证开启 + token 非法 → 拒绝（4003）
3. 正常握手 → session.ready
4. 一整轮：audio → asr.final → task.start → task.step → task.final → tts.chunk → tts.end
5. 播报中打断 → tts.end 的 interrupted=True
6. 空转写（没听清）→ empty_text 错误，但会话不断
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import voice_ws


def _settings(**over) -> SimpleNamespace:
    """只含 WS 端点用到的字段的假配置。

    ⚠️ 给 settings 加字段时要同步加到这里：SimpleNamespace 不会跟着
    config.Settings 走，漏一个字段的表现是 WS 里抛 AttributeError，
    然后被包成 error 事件发回来——测试只在等 tts.end，就会永久挂住
    （2026-10-28 实测，加了 default_start_url 之后挂死 10 分钟）。
    """
    base = dict(
        voice_enabled=True,
        auth_enabled=False,
        asr_model="small",
        tts_voice="zh-CN-XiaoxiaoNeural",
        voice_max_mb=20,
        app_port=8000,
        default_start_url="",  # 空 = 用 127.0.0.1:app_port 拼（宿主机模式）
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(voice_ws.router)
    monkeypatch.setattr(voice_ws, "settings", _settings())
    return TestClient(app)


@pytest.fixture
def auth_client(monkeypatch):
    """认证开启的版本：auth_enabled=True，其余同 client。"""
    app = FastAPI()
    app.include_router(voice_ws.router)
    monkeypatch.setattr(voice_ws, "settings", _settings(auth_enabled=True))
    return TestClient(app)


def test_first_frame_must_be_session_start(client):
    """首帧乱发 → 4001 关闭，并且带 error 说明。

    为什么要卡首帧：不卡的话任何进程都能连上占资源，
    而且"什么时候认证"会变成每个处理分支都要记一遍的事。
    """
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "hello"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "bad_handshake"


def test_invalid_token_rejected(auth_client, monkeypatch):
    """认证开启 + token 非法 → unauthorized（4003）。"""

    def fake_verify(token: str) -> int:
        from app.auth.service import AuthError

        raise AuthError("token 无效或已过期")

    monkeypatch.setattr(voice_ws, "verify_token", fake_verify)
    with auth_client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start", "token": "bad"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "unauthorized"


def test_ready_after_handshake(client, monkeypatch):
    """正常握手 → session.ready，带 session_id 和 asr_model。"""
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start"})
        msg = ws.receive_json()
        assert msg["type"] == "session.ready"
        assert msg["asr_model"] == "small"
        assert msg["session_id"]


def _patch_pipeline(monkeypatch, *, text="打开产品列表页", answer="一共有 6 件商品", chunks=3):
    """把 ASR / 任务 / 事件 / TTS 四条外部依赖全换成假的。"""

    def fake_transcribe(data: bytes) -> dict:
        return {"text": text, "duration_s": 1.2}

    async def fake_submit(**kwargs) -> str:
        assert kwargs["goal"] == text
        assert kwargs["require_approval"] is True, "语音会话不能天然免审批"
        return "task-fake"

    async def fake_follow(task_id):
        yield {"event": "task.step", "step": 1, "action": "goto 首页", "ok": True}
        yield {"event": "task.final", "task_id": task_id, "status": "success", "answer": answer}

    async def fake_stream(text_, voice_, *, max_chars=500):
        import asyncio

        for i in range(chunks):
            await asyncio.sleep(0.01)  # 让 reader 有机会插进来（打断测试需要）
            yield f"mp3-{i}".encode()

    monkeypatch.setattr(voice_ws.asr, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice_ws.task_runner, "submit", fake_submit)
    monkeypatch.setattr(voice_ws.events, "follow", fake_follow)
    monkeypatch.setattr(voice_ws.tts, "synthesize_stream", fake_stream)


def test_full_round_event_sequence(client, monkeypatch):
    """一整轮的事件顺序：asr.final → task.start → task.step → task.final → tts.chunk… → tts.end。"""
    _patch_pipeline(monkeypatch)
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start"})
        assert ws.receive_json()["type"] == "session.ready"

        audio = base64.b64encode(b"x" * 2048).decode()
        ws.send_json({"type": "audio.append", "seq": 0, "data": audio, "final": True})

        seen: list[str] = []
        while True:
            msg = ws.receive_json()
            # 服务端发 error 之后不会有 tts.end：不在这里立刻失败，
            # receive_json 会一直阻塞，表现为"测试挂死"而不是"测试失败"
            assert msg["type"] != "error", msg
            seen.append(msg["type"])
            if msg["type"] == "asr.final":
                assert msg["text"] == "打开产品列表页"
            if msg["type"] == "task.start":
                assert msg["task_id"] == "task-fake"
                assert msg["goal"] == "打开产品列表页"
            if msg["type"] == "tts.end":
                assert msg["interrupted"] is False
                break

        # 顺序断言（不是集合断言）：事件流错序前端会渲染错
        assert seen == [
            "asr.final",
            "task.start",
            "task.step",
            "task.final",
            "tts.chunk",
            "tts.chunk",
            "tts.chunk",
            "tts.end",
        ], seen


def test_barge_in_stops_playback(client, monkeypatch):
    """播报中发 interrupt → 立刻停，tts.end 带 interrupted=True。

    barge-in 是"实时对话"和"点播按钮"的体感分界线（P6 决策 4）。
    断言 interrupted 而不只是"提前结束"：前端要靠这个标志决定
    "这段播报是被用户打断的"，还是"播完了"。
    """
    _patch_pipeline(monkeypatch, chunks=50)  # 块多，保证来得及打断
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start"})
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_json(
            {"type": "audio.append", "seq": 0, "data": base64.b64encode(b"x" * 2048).decode(), "final": True}
        )

        got_chunk = False
        while True:
            msg = ws.receive_json()
            if msg["type"] == "tts.chunk" and not got_chunk:
                got_chunk = True
                ws.send_json({"type": "interrupt"})  # 说"别念了"
            if msg["type"] == "tts.end":
                assert msg["interrupted"] is True
                assert msg["seq"] < 50, "打断应该在播完之前生效"
                break


def test_empty_transcript_does_not_kill_session(client, monkeypatch):
    """没听清 → 报 empty_text，但会话还活着（还能再说一句）。"""
    _patch_pipeline(monkeypatch, text="", answer="")
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start"})
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_json(
            {"type": "audio.append", "seq": 0, "data": base64.b64encode(b"x" * 2048).decode(), "final": True}
        )
        assert ws.receive_json()["type"] == "asr.final"
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "empty_text"

        # 关键：会话没断，ping 还能通
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_too_short_audio_is_rejected_before_asr(client, monkeypatch):
    """不到 1KB 的音频直接拒，不去跑模型（点了麦克风立刻又停的典型情况）。"""
    _patch_pipeline(monkeypatch)
    with client.websocket_connect("/api/v1/voice/ws") as ws:
        ws.send_json({"type": "session.start"})
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_json(
            {"type": "audio.append", "seq": 0, "data": base64.b64encode(b"x" * 100).decode(), "final": True}
        )
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "too_short"
