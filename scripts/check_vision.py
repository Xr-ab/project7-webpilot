"""P6a 端到端验收：canvas 页任务 → 视觉兜底真被触发 → 答案和图一致。

它是什么：A5 的验收脚本，跑**真浏览器 + 真视觉模型 + 真 worker**，不是单测。
在系统里演的角色：单测证明"代码路径对"，这个脚本证明"这件事真的能成"——
canvas 页面 DOM 里没有任何可读内容，Agent 唯一的答案来源就是视觉模型对截图的描述。

前置条件（很重要，否则断言会以"没触发视觉"的形式失败）：
    API 和 worker 都必须带 VISION_ENABLED=true 启动——默认是关的（保评测基线）：
        VISION_ENABLED=true python -m uvicorn app.main:app --port 8000
        VISION_ENABLED=true python -m arq app.worker.WorkerSettings
    刻意不开 VISION_AUTO_FALLBACK 也行：本脚本的任务级 vision=true 会强制触发。

断言（4 条，全部通过才算 PASS）：
    1. 任务以 success 收尾
    2. 答案里的最高月份和数值与 canvas 上画的一致（5 月 / 4700）
    3. trace 里出现 vision.used=true 且 trigger="task"
    4. 视觉描述以 [观察] 通道进了当轮 prompt（decision.notice 含"视觉参考"）

用法：
    python scripts/check_vision.py
"""
from __future__ import annotations

import os
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 复用批处理脚本的共用工具（P5b-④ 立的规矩：payload 构造和 token 只写一处）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import auth_headers, ensure_token, task_payload  # noqa: E402

# BASE 可用环境变量覆盖：容器里跑时 API 调用和 Chromium 的 start_url
# 都必须走服务名 http://api:8000（worker 容器的 127.0.0.1 不是 api，
# 这是容器化最经典的一跤，2026-10-28 实测踩过）
BASE = os.environ.get("WEBPILOT_BASE", "http://127.0.0.1:8000")
PAGE = f"{BASE}/testsite/canvas.html"
# 项目根：脚本在 scripts/ 下，parents[1] 就是根（runs/ 在这里）
ROOT = Path(__file__).resolve().parents[1]
# canvas 上画的标准答案（见 testsite/canvas.html 的注释）
EXPECT_MONTH = "5"
EXPECT_VALUE = "4700"

TOKEN = ""


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers=auth_headers(TOKEN))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **auth_headers(TOKEN)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    global TOKEN
    TOKEN = ensure_token(BASE)

    payload = task_payload(
        "这张销售看板画在 canvas 里。请根据页面内容回答：哪个月的销量最高？最高是多少？"
        "只回答月份和数字，不要点任何东西。",
        PAGE,
        require_approval=False,
    )
    payload["vision"] = True  # 任务级强制开视觉（P6a 决策 1 条件 3）

    task_id = _post("/api/v1/tasks", payload)["task_id"]
    print(f"提交任务：{task_id}")

    # 轮询到终态。超时给 180s：视觉一步 3~10s，加上浏览器启动和模型决策
    deadline = time.time() + 180
    status, answer = "", ""
    while time.time() < deadline:
        data = _get(f"/api/v1/tasks/{task_id}")
        status = data.get("status") or ""
        answer = data.get("answer") or ""
        if status in ("success", "error", "timeout", "loop", "invalid_action", "nav_failed"):
            break
        time.sleep(2)

    trace = _get(f"/api/v1/tasks/{task_id}/trace").get("trace") or []
    vision_rows = [row for row in trace if (row.get("vision") or {}).get("used")]

    # 断言 4 的数据源必须换：decision（含当轮 notice / snapshot_text）**只写 trace.json，
    # 不入 DB**——数据库存的是可聚合的固定列，decision 是一条几十 KB 的完整上下文，
    # 进表等于让 task_steps 变成 blob 仓库。所以这里直接读磁盘上那份真相。
    # （这一条和 vision 列是两类问题：vision 我们加了列，decision 是**刻意不入库**的，
    #   所以脚本读哪个源要分清，别指望 API 什么都有。）
    json_trace_path = ROOT / "runs" / task_id / "trace.json"
    json_trace = (
        json.loads(json_trace_path.read_text(encoding="utf-8"))
        if json_trace_path.exists()
        else []
    )
    notices = [(row.get("decision") or {}).get("notice") or "" for row in json_trace]

    print(f"\n状态：{status}")
    print(f"答案：{answer!r}")
    print(f"步数：{len(trace)}；触发视觉的步数：{len(vision_rows)}")
    for row in vision_rows:
        v = row["vision"]
        print(f"  step {row.get('step')}: trigger={v.get('trigger')} {v.get('ms')}ms")
        print(f"    视觉描述：{(v.get('summary') or '')[:160]}")

    checks: list[tuple[str, bool, str]] = [
        ("任务成功收尾", status == "success", f"实际 {status}"),
        (
            "答案与图一致",
            EXPECT_MONTH in answer and EXPECT_VALUE in answer,
            f"期望含『{EXPECT_MONTH} 月』与『{EXPECT_VALUE}』，实际 {answer!r}",
        ),
        (
            "trace 有 vision.used=true（trigger=task）",
            any(v["vision"].get("trigger") == "task" for v in vision_rows),
            "没有任何一步记录到任务级视觉调用——先确认 worker 是带 VISION_ENABLED=true 起的",
        ),
        (
            "视觉描述进了 prompt（notice 含『视觉参考』）",
            any("视觉参考" in n for n in notices),
            "notice 里没找到视觉描述，检查 step_observe 的注入",
        ),
    ]

    print()
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" —— {detail}"))
        failed += 0 if ok else 1

    print(f"\n结果：{len(checks) - failed}/{len(checks)} 通过；任务 {task_id}（产物在 runs/{task_id}）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}：{exc.read().decode('utf-8', 'ignore')[:300]}")
        sys.exit(2)
