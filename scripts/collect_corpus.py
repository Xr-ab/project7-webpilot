"""语料采集 CLI（P4-N2）：批量跑"读页面 + 回答"任务，给知识库攒语料。

跑法：
    <venv python> scripts/collect_corpus.py              # 跑全部采集任务（20 个）
    <venv python> scripts/collect_corpus.py --limit 3    # 只跑前 3 个（试水）
    <venv python> scripts/collect_corpus.py --only p2,t_love   # 指定编号
    <venv python> scripts/collect_corpus.py --no-flush    # 只跑任务不沉淀

前置条件（缺一个都会失败，报错信息会指出来）：
    1. API 服务起着：uvicorn app.main:app --port 8000
    2. arq worker 起着：QUEUE_ENABLED=true 时任务靠它消费，否则永远 queued
    3. 目标域名在 ALLOWED_HOSTS 里

为什么"直接给目标页 URL"而不是"从首页翻过去"：
    翻页任务要多步、失败率高（P1 的 R2 就栽在这），而采集只需要"页面的内容"。
    start_url 直接指向第 N 页，任务退化成"读当前页 → 回答"，1~3 步就完成。
    语料采集不是能力评测，别把两件事混在一起。

为什么每个页面换个 goal 措辞：goal 会进知识节点（"任务：X / 答案：Y"）。
    20 个页面用同一句话，任务级节点内容全同会被 content_hash 去重挡掉——
    留着不同措辞既符合真实使用场景，也避免自己给自己制造重复。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

BASE = "http://127.0.0.1:8000"

from common import task_payload  # noqa: E402  脚本直跑时 sys.path[0] 就是 scripts/
# 采集专用账号（幂等：已注册过就登录）。密码只在本地开发用
EMAIL = "collect@webpilot.dev"
PASSWORD = "collect-corpus-2026"

# 每个任务最多等多久：单页任务正常 5~20 秒，留 90 秒余量
WAIT_LIMIT = 90
POLL_INTERVAL = 2.0

# 采集清单：(编号, goal, start_url)
# 统一用"列出作者"这种单步任务；20 个页面 = 10 页翻页 + 10 个标签页
_TAG_GOALS = [
    "把这个页面里出现过的作者名字都列出来",
    "这一页上的名言分别是谁说的？请把作者都列出来",
    "列出本页所有名言的作者",
    "这一页出现了哪些作者？",
    "请提取本页每一条名言的作者名字",
    "本页的名言都出自谁？把作者名字列全",
]

COLLECT_TASKS: list[tuple[str, str, str]] = []

# 10 个翻页
for i in range(1, 11):
    url = "https://quotes.toscrape.com/" if i == 1 else f"https://quotes.toscrape.com/page/{i}/"
    COLLECT_TASKS.append((f"p{i}", _TAG_GOALS[(i - 1) % len(_TAG_GOALS)], url))

# 10 个标签页
_TAGS = [
    "love", "inspiration", "life", "humor", "books",
    "reading", "friendship", "friends", "truth", "wisdom",
]
for i, tag in enumerate(_TAGS):
    COLLECT_TASKS.append(
        (f"t_{tag}", _TAG_GOALS[(i + 3) % len(_TAG_GOALS)], f"https://quotes.toscrape.com/tag/{tag}/")
    )


# ---------------------------------------------------------------- HTTP（带 token）


def _request(path: str, *, payload: dict | None = None, token: str = "") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login() -> str:
    """拿 token：先尝试注册，已存在则登录。认证关闭时返回空串（本地单人模式）。"""
    creds = {"email": EMAIL, "password": PASSWORD}
    try:
        return _request("/api/v1/auth/register", payload=creds)["token"]
    except urllib.error.HTTPError as exc:
        if exc.code == 409:  # 已注册
            return _request("/api/v1/auth/login", payload=creds)["token"]
        if exc.code == 403:  # AUTH_ENABLED=false
            return ""
        raise


# ---------------------------------------------------------------- 跑任务


def run_one(token: str, goal: str, url: str) -> dict:
    """提交 + 轮询到终态。返回 meta（含 status/answer）。"""
    # require_approval 默认 False 收进 common.task_payload（批量无人值守，
    # 卡在等人审批上采集就停了，P5a 教训见 common.py）
    task_id = _request(
        "/api/v1/tasks",
        payload=task_payload(goal, url),
        token=token,
    )["task_id"]
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        meta = _request(f"/api/v1/tasks/{task_id}", token=token)
        if meta["status"] not in ("queued", "running"):
            return meta
        time.sleep(POLL_INTERVAL)
    return {"task_id": task_id, "status": "collect_timeout", "answer": "", "note": "等待超时"}


async def flush(task_ids: list[str]) -> dict:
    """把跑完的任务沉淀进知识库（复用 N1 的 flush_task）。"""
    from app.knowledge.service import count_nodes, flush_task

    written = 0
    for tid in task_ids:
        try:
            result = await flush_task(tid)
            written += result["written"]
        except Exception as exc:
            print(f"  沉淀失败 {tid}：{exc}")
    return {"written": written, "nodes": await count_nodes()}


def main() -> int:
    parser = argparse.ArgumentParser(description="批量采集语料")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个")
    parser.add_argument("--only", type=str, default="", help="逗号分隔的编号，如 p2,t_love")
    parser.add_argument("--no-flush", action="store_true", help="只跑任务，不沉淀")
    args = parser.parse_args()

    tasks = COLLECT_TASKS
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        tasks = [t for t in tasks if t[0] in wanted]
        if not tasks:
            print(f"没有匹配的编号，可选：{[t[0] for t in COLLECT_TASKS]}")
            return 1
    if args.limit:
        tasks = tasks[: args.limit]

    try:
        token = login()
    except urllib.error.URLError as exc:
        print(f"连不上 API（{BASE}）：{exc}\n先启动服务：cd backend && python -m uvicorn app.main:app --port 8000")
        return 1
    print(f"登录成功（{'带 token' if token else '认证关闭'}）；本次采集 {len(tasks)} 个页面\n")

    done: list[str] = []
    failed: list[str] = []
    for idx, (code, goal, url) in enumerate(tasks, 1):
        print(f"[{idx}/{len(tasks)}] {code}  {url}")
        try:
            meta = run_one(token, goal, url)
        except Exception as exc:
            print(f"    ✗ 提交/轮询失败：{exc}")
            failed.append(code)
            continue
        status = meta.get("status")
        answer = (meta.get("answer") or "").replace("\n", " ")[:80]
        if status == "success":
            print(f"    ✓ {meta.get('steps', '?')} 步 | {answer}")
            done.append(meta["task_id"])
        else:
            print(f"    ✗ {status} | {answer or meta.get('note', '')}")
            failed.append(code)

    print(f"\n任务结果：成功 {len(done)} / 失败 {len(failed)}")
    if failed:
        print(f"失败清单：{failed}")

    if done and not args.no_flush:
        import asyncio

        stats = asyncio.run(flush(done))
        print(f"沉淀完成：新增 {stats['written']} 条 | 现有节点 {stats['nodes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
