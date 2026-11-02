"""并发压测（M6）：N 个任务同时提交，记录成功率与内存峰值。

跑法：
    <venv python> scripts/stress.py --count 9

它测什么：
1. **吞吐与排队**：worker 的 max_jobs=3（用户拍板，免费档 LLM 有限流、
   Chromium 每任务吃 200~400MB），提交 9 个任务应该呈现"3 个并行、
   其余排队"的节奏——总墙钟 ≈ 单任务耗时 × ⌈N/3⌉，明显偏离就说明并发模型有问题。
2. **内存峰值**：worker 进程 + 它的 Chromium 子进程树的 RSS 总和。
   预期增量 ≈ 并发数 × 单 Context 内存；如果压测中内存一路涨不回落，
   说明 Context 没关干净（泄漏）。
3. **并发下的成功率**：单跑 75% 和 9 个并发跑 75% 意义完全不同——
   并发会暴露 LLM 限流（429）、Redis 队列堆积、Chromium 崩溃这类单跑看不到的问题。

为什么用本地测试站压：真实站点慢且贵，会把"压测噪声"和"站点波动"混在一起；
测试站任务 2~10 秒、行为稳定，测的才是系统本身的承载力。
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
BASE = "http://127.0.0.1:8000"

from common import auth_headers, ensure_token, task_payload  # noqa: E402

# 评测账号 token：M4 之后任务接口要自证身份（见 common.ensure_token）
_TOKEN = ""

# 轮询全部任务到终态的总上限。9 个任务 × 180s 理论最坏 ≈ 1000s，实际远用不到
WAIT_LIMIT = 900

# 压测任务集：本地测试站里两条不同形态的路径（读取判断 / 搜索交互）。
# 提交时轮转取用，让并发批次里的任务类型混合，更接近真实使用
STRESS_TASKS = [
    (
        "T3",
        "在产品列表页找出所有价格低于 100 元的商品名称",
        "http://127.0.0.1:8000/testsite/list.html",
    ),
    (
        "T4",
        "在商品搜索页搜索 usb，告诉我一共几条结果",
        "http://127.0.0.1:8000/testsite/search.html",
    ),
]


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers=auth_headers(_TOKEN))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json", **auth_headers(_TOKEN)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_worker_pid() -> int | None:
    """找 worker 进程：python 进程且命令行同时含 arq 和 WorkerSettings。

    按 cmdline 找而不是按名字：python.exe 一大堆，只有命令行能区分出
    "这是个 arq worker"（API 进程的 cmdline 是 uvicorn，不会误伤）。
    """
    import psutil

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "python" in cmd.lower() and "arq" in cmd and "WorkerSettings" in cmd:
                return proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def tree_rss_mb(pid: int) -> float:
    """worker 进程 + 全部子进程（Chromium 树）的 RSS 总和，单位 MB。

    为什么要算整棵树：worker 的 python 进程本身只占百来 MB，
    内存大头在它启动的 Chromium 里（每个任务的独立 Context 也在其中）。
    只看 python 进程的 RSS 等于没测。
    """
    import psutil

    parent = psutil.Process(pid)
    total = parent.memory_info().rss
    for child in parent.children(recursive=True):
        try:
            total += child.memory_info().rss
        except psutil.NoSuchProcess:
            pass  # 采样间隙里子进程退出了，跳过即可
    return total / 1024 / 1024


async def sample_memory(pid: int, stop: asyncio.Event, samples: list[float]) -> None:
    """后台采样协程：每秒记一次内存，直到收到停止信号。

    psutil 是同步库，用 to_thread 包住避免堵住事件循环
    （M2 的教训：async 里塞同步阻塞调用）。
    ⚠️ except 必须写 asyncio.TimeoutError：Python 3.10 里它和内建
    TimeoutError 是两个类，到 3.11 才统一——写错的话超时异常会从
    这里逃出去把整个压测炸掉（实测踩过）。
    """
    while not stop.is_set():
        mb = await asyncio.to_thread(tree_rss_mb, pid)
        samples.append(mb)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def pct(values: list[float], p: float) -> float:
    """线性插值分位数（与 /api/v1/stats 的 percentile_cont 口径一致）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = p * (len(ordered) - 1)
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


async def main_async(count: int) -> int:
    global _TOKEN
    try:
        _get("/health")
    except (urllib.error.URLError, TimeoutError):
        print(f"API 没起：{BASE} 打不开。先跑 uvicorn 再压测。")
        return 1
    _TOKEN = ensure_token(BASE)  # 任务接口要 token（health 不要，过了不代表能提交）

    worker_pid = find_worker_pid()
    if worker_pid is None:
        print("找不到 worker 进程（需要先跑：-m arq app.worker.WorkerSettings）。")
        return 1
    print(f"worker pid={worker_pid}")

    stop = asyncio.Event()
    samples: list[float] = []
    sampler = asyncio.create_task(sample_memory(worker_pid, stop, samples))
    baseline_mb = samples[0] if samples else await asyncio.to_thread(tree_rss_mb, worker_pid)

    # ---- 并发提交：asyncio.gather 让 N 个 POST 同时刻发出去 ----
    # 轮转取任务模板：count=9 时 T3/T4 各占一半左右，类型混合
    print(f"并发提交 {count} 个任务 …")
    started = time.perf_counter()

    async def submit_one(i: int) -> str:
        code, goal, url = STRESS_TASKS[i % len(STRESS_TASKS)]
        # require_approval 默认 False 收进 common.task_payload：
        # 压测卡在等人审批上测出来的 P95 毫无意义（P5a 任务级参数）
        data = await asyncio.to_thread(_post, "/api/v1/tasks", task_payload(goal, url))
        return data["task_id"]

    task_ids = list(await asyncio.gather(*[submit_one(i) for i in range(count)]))
    submit_done = time.perf_counter() - started
    print(f"  全部入队（{submit_done:.2f}s）：{task_ids}")

    # ---- 轮询到全部终态 ----
    metas: dict[str, dict] = {}
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        for tid in task_ids:
            if tid in metas:
                continue
            meta = await asyncio.to_thread(_get, f"/api/v1/tasks/{tid}")
            if meta["status"] not in ("queued", "running"):
                metas[tid] = meta
                print(f"  [{len(metas)}/{count}] {tid} → {meta['status']}（{meta.get('elapsed_s')}s）")
        if len(metas) == count:
            break
        await asyncio.sleep(1.5)

    stop.set()
    await sampler

    wall = time.perf_counter() - started
    peak_mb = max(samples) if samples else 0.0
    timeout_ids = [tid for tid in task_ids if tid not in metas]

    # ---- 汇总 ----
    finished = list(metas.values())
    successes = [m for m in finished if m["status"] == "success"]
    elapsed_list = [m["elapsed_s"] for m in finished if m.get("elapsed_s") is not None]
    dist: dict[str, int] = {}
    for m in finished:
        dist[m["status"]] = dist.get(m["status"], 0) + 1

    rate = len(successes) / len(finished) * 100 if finished else 0.0
    print("\n" + "=" * 64)
    print(f"提交 {count} | 完成 {len(finished)} | 超时未归 {len(timeout_ids)}")
    print(f"状态成功率：{len(successes)}/{len(finished)} = {rate:.1f}%")
    print(f"状态分布：{dist}")
    if elapsed_list:
        print(
            f"单任务耗时：P50={pct(elapsed_list, 0.5):.1f}s  "
            f"P95={pct(elapsed_list, 0.95):.1f}s  max={max(elapsed_list):.1f}s"
        )
    print(f"总墙钟：{wall:.1f}s（理论下限 ≈ 最慢任务 × ⌈{count}/{3}⌉ 批）")
    print(f"内存：基线 {baseline_mb:.0f}MB → 峰值 {peak_mb:.0f}MB（增量 {peak_mb - baseline_mb:.0f}MB，含 Chromium 树）")

    if timeout_ids:
        print(f"超时未归：{timeout_ids}")

    # ---- 落盘 ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT_ROOT / "runs" / f"stress_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "# 并发压测报告（M6）",
        "",
        f"- worker pid：{worker_pid}（max_jobs=3）",
        f"- 提交：{count} 个（T3/T4 轮转），全部入队耗时 {submit_done:.2f}s",
        f"- 完成：{len(finished)}，超时未归：{len(timeout_ids)}",
        f"- 状态成功率：{len(successes)}/{len(finished)} = {rate:.1f}%",
        f"- 状态分布：{json.dumps(dist, ensure_ascii=False)}",
        f"- 单任务耗时 P50/P95/max："
        f"{pct(elapsed_list, 0.5):.1f}s / {pct(elapsed_list, 0.95):.1f}s / {max(elapsed_list):.1f}s"
        if elapsed_list
        else "- 无耗时数据",
        f"- 总墙钟：{wall:.1f}s",
        f"- 内存：基线 {baseline_mb:.0f}MB → 峰值 {peak_mb:.0f}MB（增量 {peak_mb - baseline_mb:.0f}MB）",
        f"- 内存采样点：{len(samples)} 个",
        "",
        "## 逐任务明细",
        "",
        "| task_id | 状态 | 步数 | 耗时s | note |",
        "|---|---|---|---|---|",
    ]
    for tid in task_ids:
        m = metas.get(tid, {"status": "timeout", "steps": 0, "note": "轮询超时未归", "elapsed_s": None})
        report_lines.append(
            f"| {tid} | {m['status']} | {m.get('steps')} | {m.get('elapsed_s')} | "
            f"{(m.get('note') or '').replace('|', '/')[:60]} |"
        )
    (out_dir / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    (out_dir / "raw.json").write_text(
        json.dumps(
            {
                "count": count,
                "wall_s": round(wall, 1),
                "baseline_mb": round(baseline_mb, 1),
                "peak_mb": round(peak_mb, 1),
                "dist": dist,
                "metas": metas,
                "memory_samples": [round(s, 1) for s in samples],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"报告：{out_dir / 'report.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="并发压测：N 个任务同时提交")
    parser.add_argument("--count", type=int, default=9, help="并发提交的任务数（默认 9）")
    return asyncio.run(main_async(max(1, parser.parse_args().count)))


if __name__ == "__main__":
    raise SystemExit(main())
