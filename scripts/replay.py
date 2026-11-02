"""回放器 CLI：对最近的历史任务做"老 prompt vs 新 prompt"决策对比。

跑法：
    <venv python> scripts/replay.py                  # 默认回放最近 4 个可回放任务
    <venv python> scripts/replay.py --recent 8       # 最近 8 个
    <venv python> scripts/replay.py --task 20261025-102357-cw9p44   # 指定任务（可重复）
    <venv python> scripts/replay.py --recent 4 --limit 5            # 每任务最多回放 5 步

前置条件：
    任务是 M5a 之后跑的（trace 里有 decision 字段）。更早的任务会被
    自动跳过并在报告里计入 skipped——历史数据不会误报成"决策一致"。

输出：
    stdout 报告 + runs/replay_<时间戳>/{report.md, raw.json}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# scripts/ 不在包里，手动把 backend 加进 import 路径（沿用 smoke_llm.py 的惯例）
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.eval.replayer import render_report, replay_task, summarize  # noqa: E402
from app.storage.files import RUNS_ROOT  # noqa: E402


def pick_recent(n: int) -> list[str]:
    """按目录名倒序挑最近 n 个"可回放"的任务。

    可回放的快速判据：trace.json 存在，且文本里含 '"decision"' 子串。
    用子串而不是解析整个 JSON——runs/ 里有几十个任务时，逐个 json.load 太慢，
    而且我们只关心"有没有这个字段"，不关心它的值。
    """
    if not RUNS_ROOT.exists():
        return []
    picked: list[str] = []
    # task_id 以时间戳开头，目录名倒序就是时间倒序（files.new_task_id 的设计）
    for path in sorted(RUNS_ROOT.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        trace_file = path / "trace.json"
        if not trace_file.exists():
            continue
        try:
            if '"decision"' not in trace_file.read_text(encoding="utf-8"):
                continue
        except OSError:
            continue
        picked.append(path.name)
        if len(picked) >= n:
            break
    return picked


async def main_async(args: argparse.Namespace) -> int:
    task_ids = list(args.task) if args.task else pick_recent(args.recent)
    if not task_ids:
        print("没有找到可回放的任务（runs/ 里没有带 decision 字段的 trace）。")
        print("先跑：scripts/eval_real.py --rounds 2 生成新轨迹，再来回放。")
        return 1

    # LLM 客户端整个回放共用一个（内部有连接池），不要每任务建一个
    from app.agent.llm import LLMClient

    llm = LLMClient()

    replays: list[dict] = []
    failures: list[str] = []
    for task_id in task_ids:
        print(f"回放 {task_id} … ", end="", flush=True)
        try:
            replay = await replay_task(task_id, llm=llm, limit=args.limit)
            replays.append(replay)
            print(f"{len(replay['results'])} 步，跳过 {replay['skipped']} 步")
        except (FileNotFoundError, ValueError) as exc:
            failures.append(f"{task_id}: {exc}")
            print(f"失败（{exc}）")

    if not replays:
        print("所有任务都回放失败，没有可汇总的结果。")
        for line in failures:
            print(f"  {line}")
        return 1

    summary = summarize(replays)
    report = render_report(replays, summary)
    print()
    print(report)

    if failures:
        print("失败任务：")
        for line in failures:
            print(f"  {line}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT_ROOT / "runs" / f"replay_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "raw.json").write_text(
        json.dumps({"summary": summary, "replays": replays}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告：{out_dir / 'report.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回放历史任务，对比老/新 prompt 的决策差异")
    parser.add_argument("--task", action="append", help="指定任务 ID（可重复传）")
    parser.add_argument("--recent", type=int, default=4, help="回放最近 N 个可回放任务（默认 4）")
    parser.add_argument("--limit", type=int, default=None, help="每个任务最多回放多少步")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
