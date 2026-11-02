"""M4 验收脚本：跑一个任务，把每一步的 thought / 动作 / 结果打出来。

跑法：
    1) 起服务：cd backend && <venv python> -m uvicorn app.main:app --port 8000
    2) <venv python> scripts/agent_demo.py --task T1

五个标准任务来自 P1 计划，M6 的评测脚本也复用这份定义（口径必须一致，
否则"成功率 60%"到底是在说什么就没人说得清）。
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
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from llama_index.core.workflow import WorkflowTimeoutError  # noqa: E402

from app.agent.llm import LLMClient  # noqa: E402
from app.agent.workflow import BrowserAgentWorkflow  # noqa: E402
from app.browser.session import BrowserSession  # noqa: E402

BASE = "http://127.0.0.1:8000/testsite"

TASKS = {
    "T1": ("从首页进入产品列表，提取所有产品名称", f"{BASE}/index.html"),
    "T2": ("打开资料填写页，姓名填张三、邮箱填 zhang@example.com、城市选福州、勾选订阅，然后提交",
           f"{BASE}/form.html"),
    "T3": ("在产品列表页找出所有价格低于 100 元的商品名称", f"{BASE}/list.html"),
    "T4": ("在商品搜索页搜索 usb，告诉我一共几条结果", f"{BASE}/search.html"),
    "T5": ("打开商品详情页，点下一页，告诉我第二页商品的名字和价格", f"{BASE}/detail.html"),
}


def server_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/index.html", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def print_trace(result: dict) -> None:
    print("\n" + "=" * 78)
    print("轨迹")
    print("=" * 78)
    for item in result["trace"]:
        flag = "OK " if item["ok"] else "ERR"
        print(f"[{item['step']:>2}] {flag} {item['action']:<34} {item['duration_ms']:>5}ms")
        if item["thought"]:
            print(f"      想：{item['thought']}")
        if item["error"]:
            print(f"      错：{item['error']}")


async def main(task: str, goal_override: str | None, url_override: str | None, show: bool) -> int:
    if not server_is_up():
        print(f"服务没起：{BASE} 打不开。先跑：cd backend && <venv python> -m uvicorn app.main:app --port 8000")
        return 1

    goal, start_url = TASKS[task]
    if goal_override:
        goal = goal_override
    if url_override:
        start_url = url_override

    print(f"任务 {task}：{goal}")
    print(f"起点：{start_url}")
    print(f"浏览器：{'可见窗口' if show else '无头模式'}\n")

    started = time.perf_counter()
    # headless=False 会真的弹出一个 Chromium 窗口，可以肉眼看着它点
    async with BrowserSession(headless=not show) as session:
        wf = BrowserAgentWorkflow(
            session=session,
            llm=LLMClient(),
            # 手动演示要一口气看到结果，别在「提交」那一步停下来等人（P5a）。
            # 想看审批行为请跑 scripts/check_approval.py——那条路专门测审批，
            # 这里关了它才看得完整条 trace。
            # 注意它和进程级 APPROVAL_ENABLED 是"与"关系，所以传 False 一定生效。
            approval_enabled=False,
        )
        try:
            result = await wf.run(goal=goal, start_url=start_url)
        except WorkflowTimeoutError:
            # 框架超时：trace 还在 wf 上，照样能拿出来看死在哪一步
            result = {
                "status": "timeout",
                "answer": "",
                "steps": wf.step_no,
                "note": f"超过 {wf.timeout_s}s 仍未结束",
                "trace": wf.trace,
            }
    elapsed = time.perf_counter() - started

    print_trace(result)
    print("\n" + "-" * 78)
    print(f"状态：{result['status']}   步数：{result['steps']}   耗时：{elapsed:.1f}s")
    if result["note"]:
        print(f"说明：{result['note']}")
    print(f"答案：{result['answer'] or '（无）'}")

    out_dir = PROJECT_ROOT / "runs" / "agent_demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{task}_{time.strftime('%H%M%S')}.json"
    out_file.write_text(
        json.dumps({"task": task, "goal": goal, "elapsed_s": round(elapsed, 2), **result},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"轨迹已存：{out_file}")
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="T1", choices=sorted(TASKS))
    parser.add_argument("--goal", default=None, help="覆盖预设任务描述，用来临时试别的指令")
    parser.add_argument("--url", default=None, help="覆盖起始页地址（必须在自己白名单里）")
    parser.add_argument("--show", action="store_true", help="弹出可见浏览器窗口，肉眼看它怎么点")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.task, args.goal, args.url, args.show)))
