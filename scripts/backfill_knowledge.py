"""知识回填 CLI（P4-N1）：把历史成功任务沉淀成 knowledge_nodes。

跑法：
    <venv python> scripts/backfill_knowledge.py                # 全量成功任务
    <venv python> scripts/backfill_knowledge.py --limit 5      # 只回填最早 5 个（试水）
    <venv python> scripts/backfill_knowledge.py --count        # 只看现状不进库

前置条件：
    1. PostgreSQL 起着（wp-postgres 容器）+ `alembic upgrade head` 已执行
    2. 首次运行会下载 bge-small-zh-v1.5 模型（约 95MB）；
       下载慢/失败时先设置镜像源：export HF_ENDPOINT=https://hf-mirror.com

为什么回填要先于自动沉淀钩子上线：
    钩子挂进 task_runner 之后，每次任务收尾都会走一遍 embed——模型加载失败
    或维度不对会直接影响任务收尾。先用离线回填把"模型能跑通、维度对得上、
    检索能返回"验证完，钩子才是安全的。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


async def main() -> int:
    parser = argparse.ArgumentParser(description="知识回填：成功任务 → knowledge_nodes")
    parser.add_argument("--limit", type=int, default=None, help="最多回填多少个任务")
    parser.add_argument("--count", action="store_true", help="只打印现有节点统计，不回填")
    args = parser.parse_args()

    from app.db import repo  # noqa: F401  仅为触发 session 模块初始化顺序
    from app.knowledge.service import backfill, count_nodes

    if args.count:
        stat = await count_nodes()
        print("现有节点：", stat or "（空）")
        return 0

    print("开始回填（首次会下载模型，约 95MB）...")
    stats = await backfill(limit=args.limit)
    print(
        f"\n回填完成：任务 {stats['total']} 个 | 写入节点 {stats['written']} 条 | "
        f"跳过 {stats['skipped']} 个 | 失败 {stats['errors']} 个"
    )
    stat = await count_nodes()
    print("按来源类型：", stat or "（空）")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
