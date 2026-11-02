"""容器栈验收（P7-D3）：验证 `docker compose up -d` 起来的那套东西**真的能用**。

它和 check_* 系列的区别：
其它脚本测的是"宿主机直接跑的进程"，这个脚本只认 **http://localhost:8080** ——
也就是用户真正会打开的那个地址。所有请求都走 nginx，于是顺带验证了反代规则：
/health、/api、/runs 有没有配错，WS 的 Upgrade 头有没有漏。

七项断言，每一项都对应一种"容器化才会犯的错"：

1. nginx → api 的 /health 通        反代没配 / 上游服务名写错
2. 前端 dist 正确托管               多阶段构建把产物放错目录（页面白屏）
3. /api 无 token 返回 401           反代把路径改写坏了，或认证被绕过
4. 提交任务能跑到 success           worker 没起 / 队列没连上 / 迁移没跑
5. 答案正确                         跑通了但跑错了（最会骗人的那种成功）
6. 截图能经 nginx 取到               runs 卷没共享（api 看不到 worker 写的文件）
7. WS 握手能建立                     nginx 没配 Upgrade 头（语音全废，且报错无信息）

跑法（栈要先起来）：
    docker compose up -d
    <venv python> scripts/check_docker.py
    <venv python> scripts/check_docker.py --base http://localhost:8080

⚠️ 第 4 项的 start_url 用的是 **http://api:8000/testsite/...**，不是 127.0.0.1。
在 worker 容器里 127.0.0.1 指向它自己，用 127.0.0.1 会连接被拒——
这是容器化最经典的一跤，所以这条断言实际上也在验证"我们没写错这个地址"。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from common import auth_headers, ensure_token, task_payload  # noqa: E402

BASE = "http://localhost:8080"
# 容器内部地址：worker 里的 Chromium 用的就是它
CONTAINER_TESTSITE = "http://api:8000/testsite/index.html"
GOAL = "从首页进入产品列表，提取所有产品名称"
WANT = ["无线鼠标", "机械键盘", "显示器", "USB 集线器", "降噪耳机", "笔记本支架"]
WAIT_LIMIT = 240

_results: list[tuple[bool, str]] = []


def check(ok: bool, title: str, detail: str = "") -> bool:
    _results.append((ok, title))
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f"\n   - {detail}" if detail else ""))
    return ok


def get(path: str, token: str = "", timeout: int = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{BASE}{path}", headers=auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(path: str, payload: dict, token: str = "") -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json", **auth_headers(token)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def wait_task(task_id: str, token: str) -> dict:
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        code, body = get(f"/api/v1/tasks/{task_id}", token)
        if code == 200:
            meta = json.loads(body)
            if meta.get("status") not in ("queued", "running"):
                return meta
        time.sleep(1.5)
    return {"status": "check_timeout", "answer": "", "steps": 0}


async def check_websocket_upgrade(token: str) -> tuple[bool, str]:
    """连一次语音 WS，只看握手能不能成。

    这一项专门盯 nginx 的 Upgrade 头：漏写时浏览器（和 websockets 库）
    拿到的是一句 "server rejected WebSocket connection: HTTP 200"，
    根本看不出是反代配错了。
    """
    import websockets

    url = BASE.replace("http://", "ws://") + "/api/v1/voice/ws"
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            # 协议要求首帧自报身份（见 backend/app/api/voice_ws.py:121）：
            # 必须是 {"type": "session.start", "token": ...}，
            # token 走首帧而不是 query，避免落进访问日志
            await ws.send(json.dumps({"type": "session.start", "token": token}))
            for _ in range(6):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                msg = json.loads(raw)
                if msg.get("type") == "session.ready":
                    return True, "收到 session.ready"
                if msg.get("type") == "error":
                    code = msg.get("code")
                    if code == 4004:
                        # 语音总开关是关的，不是反代的锅
                        return True, f"语音被禁用（code={code}），握手本身是通的"
                    return False, f"服务端报错：{msg}"
    except Exception as exc:  # noqa: BLE001 —— 这里就是要看失败长什么样
        return False, f"{type(exc).__name__}: {exc}"
    return False, "握手成功但没有收到 session.ready"


def main(skip_task: bool) -> int:
    # 1) nginx → api 的最小脉路
    try:
        code, body = get("/health", timeout=15)
        check(code == 200 and b"ok" in body, "nginx 反代 /health 到 api", f"HTTP {code}")
    except Exception as exc:  # noqa: BLE001
        check(False, "nginx 反代 /health 到 api", f"{type(exc).__name__}: {exc}")
        print("\n栈没起来。先跑：docker compose up -d && docker compose ps")
        return 1

    # 2) 前端静态产物
    code, body = get("/")
    html = body.decode("utf-8", "replace")
    check(code == 200 and 'id="root"' in html, "前端 dist 已正确托管", f"HTTP {code}，{len(body)} 字节")

    # 3) 认证没被反代绕过去
    code, _ = get("/api/v1/tasks")
    check(code == 401, "/api 无 token 返回 401（认证生效）", f"HTTP {code}")

    token = ensure_token(BASE)
    check(bool(token), "登录测试账号", f"token 长度 {len(token)}")

    # 7) WS 握手先测：它不依赖任务，快
    ok, detail = asyncio.run(check_websocket_upgrade(token))
    check(ok, "语音 WS 握手经 nginx 建立（Upgrade 头）", detail)

    if skip_task:
        print("\n--skip-task：跳过任务链路三项")
    else:
        # 4) 任务链路：提交 → 队列 → worker → 落盘
        code, body = post_json("/api/v1/tasks", task_payload(GOAL, CONTAINER_TESTSITE), token)
        task_id = body.get("task_id", "")
        if not check(code == 200 and bool(task_id), "提交任务", f"HTTP {code} {body}"):
            return _summary()
        print(f"   任务 {task_id} 已提交，等待终态…")

        meta = wait_task(task_id, token)
        check(
            meta.get("status") == "success",
            "任务跑到 success（worker + 队列 + 迁移都通）",
            f"status={meta.get('status')} steps={meta.get('steps')} note={str(meta.get('note'))[:120]}",
        )

        answer = meta.get("answer", "") or ""
        missing = [w for w in WANT if w not in answer]
        check(not missing, "答案包含全部 6 个商品名", f"缺 {missing}" if missing else answer[:60])

        # 6) 截图：验证 runs 卷在 api / worker 之间是共享的
        code, body = get(f"/api/v1/tasks/{task_id}/trace", token)
        trace = json.loads(body).get("trace", []) if code == 200 else []
        shots = [row.get("screenshot_url") or row.get("screenshot") for row in trace]
        shots = [s for s in shots if s]
        if not check(bool(shots), "trace 里有截图引用", f"trace {len(trace)} 行"):
            pass
        else:
            shot = shots[0]
            path = shot if shot.startswith("/") else f"/runs/{task_id}/{shot}"
            code, img = get(path)
            check(
                code == 200 and len(img) > 5000,
                "截图经 nginx 的签名 URL 可取（runs 卷共享）",
                f"HTTP {code}，{len(img)} 字节，path={path[:60]}",
            )

    return _summary()


def _summary() -> int:
    passed = sum(1 for ok, _ in _results if ok)
    print(f"\n结果：{passed}/{len(_results)} 通过")
    if passed == len(_results):
        print("容器栈验收通过。")
        return 0
    print("失败项见上。排查顺序：docker compose ps → docker compose logs api --tail 50")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="覆盖入口地址（默认 http://localhost:8080）")
    parser.add_argument("--skip-task", action="store_true", help="只测静态与反代，不跑任务")
    args = parser.parse_args()
    if args.base:
        BASE = args.base.rstrip("/")
    raise SystemExit(main(args.skip_task))
