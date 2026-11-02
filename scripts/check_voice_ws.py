"""P6b 端到端验收：语音会话一轮完整闭环（真 ASR + 真任务 + 真 TTS）。

它是什么：B5 的验收脚本。用 websockets 客户端连 `/api/v1/voice/ws`，
上传一段**机器合成的语音**（不用真麦克风，脚本才能无人值守跑），
然后盯着事件流把每一步断言掉。

为什么用 edge-tts 生成测试音频而不是预录音频文件：
- 不用往仓库里塞二进制；文本改一个字就能造出新样本；
- edge-tts 是现成依赖（TTS 本来就在用），零新增成本。
合成出来是 mp3，ASR 的 av 解码器直接吃得下。

两轮，验两件事：
  第 1 轮：完整闭环——asr.final → task.start → task.step* → task.final →
          tts.chunk* → tts.end(interrupted=False)，并核对转写文本里有关键词。
  第 2 轮：**打断**——第一块 tts.chunk 一到就发 interrupt，
          断言 tts.end.interrupted=True。

前置（和 check_vision.py 一样）：
    API 和 worker 都要跑起来，且语音开着（VOICE_ENABLED 默认 true，不用额外设）：
        python -m uvicorn app.main:app --port 8000
        python -m arq app.worker.WorkerSettings
    第一次跑会加载 ASR 模型（约 10 秒），脚本超时给得比较宽。

用法：
    python scripts/check_voice_ws.py
"""
from __future__ import annotations

import os
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ensure_token  # noqa: E402

import websockets  # noqa: E402

# BASE 可用环境变量覆盖：容器里跑时 API 调用和 Chromium 的 start_url
# 都必须走服务名 http://api:8000（worker 容器的 127.0.0.1 不是 api，
# 这是容器化最经典的一跤，2026-10-28 实测踩过）
BASE = os.environ.get("WEBPILOT_BASE", "http://127.0.0.1:8000")
WS_URL = "ws://127.0.0.1:8000/api/v1/voice/ws"
ROOT = Path(__file__).resolve().parents[1]
AUDIO_PATH = ROOT / "runs" / "voice_probe" / "sentence.mp3"

# 这句既好念又好查：ASR 认错个把字，关键词"列表"通常还在
SENTENCE = "打开产品列表页面"
KEYWORD = "列表"


async def make_audio() -> bytes:
    """用 edge-tts 合成一句测试语音（有缓存就不重复合成）。"""
    if AUDIO_PATH.exists() and AUDIO_PATH.stat().st_size > 1000:
        return AUDIO_PATH.read_bytes()

    import edge_tts

    AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(SENTENCE, "zh-CN-XiaoxiaoNeural")
    with open(AUDIO_PATH, "wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
    return AUDIO_PATH.read_bytes()


async def recv(ws, timeout: float = 60.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def send_sentence(ws, audio: bytes, *, chunk_size: int = 16 * 1024) -> None:
    """分块推一句音频，最后一块带 final=True。"""
    seq = 0
    for start in range(0, len(audio), chunk_size):
        piece = audio[start : start + chunk_size]
        final = start + chunk_size >= len(audio)
        await ws.send(
            json.dumps(
                {
                    "type": "audio.append",
                    "seq": seq,
                    "data": base64.b64encode(piece).decode("ascii"),
                    "final": final,
                }
            )
        )
        seq += 1


async def run_round(ws, audio: bytes, *, interrupt: bool) -> dict:
    """说一句，收集事件直到 tts.end。返回统计信息。"""
    started = time.perf_counter()
    await send_sentence(ws, audio)

    stats = {
        "asr_text": "",
        "task_id": "",
        "steps": 0,
        "final": {},
        "tts_chunks": 0,
        "tts_end": {},
        "interrupted_sent": False,
    }
    while True:
        # 单条消息最多等 120s（任务可能跑十几步）；tts 也要等
        msg = await recv(ws, timeout=120.0)
        mtype = msg.get("type")

        if mtype == "error":
            print(f"  [error] {msg.get('code')}: {msg.get('message')}")
            continue
        if mtype == "asr.final":
            stats["asr_text"] = msg.get("text") or ""
        elif mtype == "task.start":
            stats["task_id"] = msg.get("task_id") or ""
        elif mtype == "task.step":
            stats["steps"] += 1
            print(f"  step {msg.get('step')}: {msg.get('action')} ok={msg.get('ok')}")
        elif mtype == "task.final":
            stats["final"] = msg
        elif mtype == "tts.chunk":
            stats["tts_chunks"] += 1
            if interrupt and not stats["interrupted_sent"]:
                stats["interrupted_sent"] = True
                await ws.send(json.dumps({"type": "interrupt"}))
        elif mtype == "tts.end":
            stats["tts_end"] = msg
            break

    stats["elapsed_s"] = round(time.perf_counter() - started, 1)
    return stats


def report(checks: list[tuple[str, bool, str]]) -> int:
    print()
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" —— {detail}"))
        failed += 0 if ok else 1
    print(f"\n结果：{len(checks) - failed}/{len(checks)} 通过")
    return 0 if failed == 0 else 1


async def main() -> int:
    token = ensure_token(BASE)
    audio = await make_audio()
    print(f"测试语音：{len(audio)} 字节（{SENTENCE}）")

    # ---- 第 1 轮：完整闭环 ----
    print("\n== 第 1 轮：完整闭环 ==")
    async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "session.start", "token": token}))
        ready = await recv(ws, timeout=10.0)
        assert ready.get("type") == "session.ready", ready
        print(f"  session.ready: {ready.get('session_id')} (asr={ready.get('asr_model')})")

        r1 = await run_round(ws, audio, interrupt=False)
        print(f"  转写：{r1['asr_text']!r}；步数 {r1['steps']}；tts 块 {r1['tts_chunks']}；耗时 {r1['elapsed_s']}s")

        # ---- 第 2 轮：打断 ----
        print("\n== 第 2 轮：播报中打断 ==")
        r2 = await run_round(ws, audio, interrupt=True)
        print(f"  转写：{r2['asr_text']!r}；tts 块 {r2['tts_chunks']}；interrupted={r2['tts_end'].get('interrupted')}")

        await ws.send(json.dumps({"type": "session.end"}))

    checks = [
        (
            "第 1 轮：转写正确（含关键词）",
            KEYWORD in r1["asr_text"],
            f"实际 {r1['asr_text']!r}，期望含『{KEYWORD}』",
        ),
        ("第 1 轮：任务跑到终态", bool(r1["final"]), "没收到 task.final"),
        (
            "第 1 轮：任务成功",
            r1["final"].get("status") == "success",
            f"实际状态 {r1['final'].get('status')!r}，note={r1['final'].get('note')!r}",
        ),
        (
            "第 1 轮：实时收到步进事件",
            r1["steps"] >= 1,
            "一步 task.step 都没收到——检查 worker 侧 events.publish_event 是否生效",
        ),
        (
            "第 1 轮：播报完整",
            r1["tts_chunks"] > 0 and r1["tts_end"].get("interrupted") is False,
            f"块数 {r1['tts_chunks']}，end={r1['tts_end']}",
        ),
        (
            "第 2 轮：打断生效",
            r2["tts_end"].get("interrupted") is True,
            f"tts.end={r2['tts_end']}（期望 interrupted=True）",
        ),
    ]
    return report(checks)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 —— 验收脚本：失败要能直接看见原因
        print(f"运行失败：{type(exc).__name__}: {exc}")
        sys.exit(2)
