"""成本口径：把"这套东西跑一整套评测要花多少钱"算成一张能对账的表。

**三个数字必须分开写，混在一起就是假账：**

1. **token 用量** —— 从 DB 聚合。唯一客观的计量事实，不依赖任何价格假设；
2. **等价人民币** —— 按同档付费模型的公示价折算，回答"换成付费档要多少钱"；
3. **实际支出** —— 本项目全程用免费档，答案是 **0 元**。

为什么"等价成本"不能省：
写 0 元没有信息量（谁都知道免费档不要钱），而招聘方/读者真正想知道的是
"这套东西到了生产上大概什么量级"。所以两个数必须并排放，并且**说清折算基准**。

为什么折算基准选 GLM-4-FlashX：
智谱官方文档（docs.bigmodel.cn/cn/guide/models/text/glm-4）里，
GLM-4-FlashX-250414 的原文定位是"**免费语言模型 GLM-4-Flash 的增强版本**"，
价格 0.1 元/百万 Tokens。同一模型家族、同一上下文窗口（128K）的付费档，
是能拿到的**最贴切**的参照物。换别的档位（Air 0.5 / Plus 5）只会把数字放大，
不代表这套代码本身的成本结构。
价格会变，所以基准做成命令行参数，不写死在报告里。

**未计费项**（不是"没有成本"，是"没有账单"）：见下方 UNBILLED。

跑法：
    <venv python> scripts/cost_report.py                  # 默认按 FlashX 折算
    <venv python> scripts/cost_report.py --price air      # 换成 Air 档对账
    <venv python> scripts/cost_report.py --since 2026-10-28   # 只看某天之后的任务
    <venv python> scripts/cost_report.py --out docs/成本口径.md

口径说明（读数字之前先看这三条）：
- 只统计**终态任务**（排除排队/运行中/待审批）。没跑完的任务 token 还没花完，
  摊进平均会让"什么时候查"影响数字——和 /stats 的口径保持一致。
- 平均成本的分母是**全部终态任务**，不是"成功任务"。为什么：失败的任务照样烧 token，
  只按成功任务算会把成本算低，那是自欺。
  （每任务 token 也给了"按成功任务"的一列，方便和成功率对照。）
- 视觉调用（glm-4v-flash）的 token **没有计入**：它走的是另一条链路，
  我们的 usage 只记了文本模型的。视觉本身也免费，但要标出来，不能装作没有。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# 折算价（元 / 百万 tokens）。来源：智谱开放平台官方文档，2026-10 核对。
# 该表给的是单一价格（未区分输入/输出），所以这里不分进出，也不假装分。
PRICES: dict[str, tuple[float, str]] = {
    "flashx": (0.1, "GLM-4-FlashX-250414（免费版 Flash 的增强版）"),
    "air": (0.5, "GLM-4-Air-250414"),
    "plus": (5.0, "GLM-4-Plus"),
}

# 分任务表最多列多少种。题面是整句中文，26 行 markdown 表已经没法读，
# 但截断必须显式写出"其余 N 种合并"，不能悄悄少几行。
TOP_GOALS = 12

# 没有账单的环节。写清"为什么不要钱"，而不是只列个名字——
# 读者要能判断"换到生产上这部分还是不是零成本"。
UNBILLED: list[tuple[str, str, str]] = [    ("页面理解（文本模型）", "GLM-4-Flash", "免费档；本项目唯一的大模型调用"),
    ("页面理解（视觉兜底）", "glm-4v-flash", "免费档；token 未计入统计（另一条链路），见脚注"),
    ("语音转写 ASR", "faster-whisper small（本地）", "本地推理，吃 CPU 不吃钱；代价是首次 464MB 模型下载"),
    ("语音合成 TTS", "edge-tts（微软）", "免费；代价是依赖外网可用性，不能离线"),
    ("向量化 / 检索", "bge-small-zh + pgvector（本地）", "本地推理；pgvector 随 PostgreSQL 自托管"),
    ("浏览器", "Playwright + Chromium（本地）", "本地进程；代价是内存（实测单任务 +280MB）"),
]


def _fmt_yuan(amount: float) -> str:
    """钱要按量级换单位，否则表格里全是 0.0004 这种看不出差异的数。

    ⚠️ 换算系数踩过一次：**1 分 = 0.01 元，所以元换分是 ×100**。
    第一版写成 ×1000，把 0.000471 元印成了「0.471 分」（真值 0.0471 分），
    表格里所有成本都大了 10 倍，而报告本身看起来毫无异常 —— 这种错只能靠手动验算抓。
    """
    if amount == 0:
        return "0 元"
    if amount < 0.01:
        fen = amount * 100
        # 小于 0.001 分就不换单位了，印成"0.000 分"反而丢信息
        return f"{fen:.3f} 分" if fen >= 0.001 else f"{amount:.6f} 元"
    return f"{amount:.4f} 元"


async def collect(since: datetime | None) -> dict:
    """从 DB 聚合。返回原始计数，不做任何价格换算（换算是展示层的事）。"""
    from sqlalchemy import Integer, func, select

    from app.db.models import Task, TaskStep
    from app.db.session import SessionLocal
    from app.status import UNFINISHED

    async with SessionLocal() as session:
        where = ~Task.status.in_(UNFINISHED)
        if since is not None:
            where = where & (Task.created_at >= since)

        overall = await session.execute(
            select(
                func.count(Task.id),
                func.coalesce(func.sum(func.cast(Task.status == "success", Integer)), 0),
                func.coalesce(func.sum(Task.steps), 0),
                func.coalesce(func.sum(Task.prompt_tokens), 0),
                func.coalesce(func.sum(Task.completion_tokens), 0),
                func.coalesce(func.avg(Task.elapsed_s), 0),
            ).where(where)
        )
        total, success, steps, prompt_tok, completion_tok, avg_elapsed = overall.one()

        # 按题目分组：goal 是题面原文，一题一行正好对应评测报告的分任务表
        by_goal_rows = await session.execute(
            select(
                Task.goal,
                func.count(Task.id),
                func.coalesce(func.sum(func.cast(Task.status == "success", Integer)), 0),
                func.coalesce(func.avg(Task.steps), 0),
                func.coalesce(func.avg(Task.prompt_tokens + Task.completion_tokens), 0),
            )
            .where(where)
            .group_by(Task.goal)
            .order_by(func.count(Task.id).desc())
        )
        by_goal = [
            {
                "goal": goal,
                "runs": int(runs),
                "success": int(ok),
                "avg_steps": float(avg_steps or 0),
                "avg_tokens": float(avg_tok or 0),
            }
            for goal, runs, ok, avg_steps, avg_tok in by_goal_rows.all()
        ]

        # 视觉调用次数：单列出来，因为它不进 token 统计。
        # ⚠️ 这里必须用 PG 的 `->>` 而不是 SQLAlchemy 的 `.astext`：
        # models 里 vision 列用的是项目自定义的 JSONType（TypeDecorator 包 JSONB），
        # 它不暴露 astext 这个 PG 方言属性，写 .astext 会直接 AttributeError。
        vision_rows = await session.execute(
            select(func.count(TaskStep.id)).where(
                TaskStep.vision.isnot(None),
                TaskStep.vision.op("->>")("used") == "true",
            )
        )
        vision_calls = int(vision_rows.scalar_one() or 0)

        # 有多少步真的记到了 usage。老任务（P2 之前）没有这个字段，
        # 直接平均会把"没记"当成"花了 0"，所以要把覆盖率报出来。
        step_rows = await session.execute(
            select(
                func.count(TaskStep.id),
                func.coalesce(func.sum(func.cast(TaskStep.prompt_tokens > 0, Integer)), 0),
            )
        )
        steps_total, steps_with_usage = step_rows.one()

    return {
        "total": int(total),
        "success": int(success),
        "steps": int(steps),
        "prompt_tokens": int(prompt_tok),
        "completion_tokens": int(completion_tok),
        "avg_elapsed": float(avg_elapsed or 0),
        "by_goal": by_goal,
        "vision_calls": vision_calls,
        "steps_total": int(steps_total),
        "steps_with_usage": int(steps_with_usage),
    }


def build_report(data: dict, price_key: str, since: datetime | None) -> str:
    unit_price, price_name = PRICES[price_key]
    total = data["total"] or 1        # 防 0 除：空库时也要能出报告
    tokens_total = data["prompt_tokens"] + data["completion_tokens"]
    cost_total = tokens_total / 1_000_000 * unit_price
    success = data["success"] or 1

    lines = [
        "# 成本口径报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 折算基准：**{price_name}**，{unit_price} 元 / 百万 tokens",
        f"- 统计范围：{'全部终态任务' if since is None else f'{since:%Y-%m-%d} 之后的终态任务'}"
        f"（排除排队/运行中/待审批）",
        "",
        "## 一、三个数字（必须分开看）",
        "",
        "| 口径 | 数值 | 说明 |",
        "|---|---|---|",
        f"| token 用量 | **{tokens_total:,}**（输入 {data['prompt_tokens']:,} + 输出 {data['completion_tokens']:,}） | 客观计量，不依赖价格假设 |",
        f"| 等价人民币 | **{_fmt_yuan(cost_total)}** | 按 {price_name} 折算；换付费档才成立 |",
        f"| 实际支出 | **0 元** | 全程免费档，没有任何计费调用 |",
        "",
        "## 二、每任务成本",
        "",
        f"- 终态任务数：**{data['total']}**（其中 success {data['success']}）",
        f"- 平均步数：**{data['steps'] / total:.1f}** / 任务",
        f"- 平均耗时：**{data['avg_elapsed']:.1f}s** / 任务",
        f"- 平均 token：**{tokens_total / total:,.0f}** / 任务"
        f"（输入 {data['prompt_tokens'] / total:,.0f} + 输出 {data['completion_tokens'] / total:,.0f}）",
        f"- 平均等价成本：**{_fmt_yuan(cost_total / total)}** / 任务",
        f"- 按成功任务算：**{tokens_total / success:,.0f}** token，"
        f"**{_fmt_yuan(cost_total / success)}** / 成功任务",
        "",
        "> 为什么两个分母都写：失败任务照样烧 token。只按成功任务算会把成本算低——"
        "那是最容易自欺的一种算法。",
        "",
        "## 三、分任务成本（按题面分组，取次数最多的前 "
        f"{TOP_GOALS} 种）",
        "",
        "| 任务 | 次数 | 成功 | 平均步数 | 平均 token | 平均等价成本 |",
        "|---|---|---|---|---|---|",
    ]

    for item in data["by_goal"][:TOP_GOALS]:
        goal = (item["goal"] or "").replace("|", "/")
        goal = goal[:34] + ("…" if len(goal) > 34 else "")
        per_cost = item["avg_tokens"] / 1_000_000 * unit_price
        lines.append(
            f"| {goal} | {item['runs']} | {item['success']} | {item['avg_steps']:.1f} | "
            f"{item['avg_tokens']:,.0f} | {_fmt_yuan(per_cost)} |"
        )

    # 长尾合并：题面是整句中文，26 行表在 markdown 里已经没法读了。
    # 但合并了就要把"合并了多少"写出来，不能悄悄截断。
    rest = data["by_goal"][TOP_GOALS:]
    if rest:
        rest_runs = sum(i["runs"] for i in rest)
        rest_tok = sum(i["avg_tokens"] * i["runs"] for i in rest)
        lines.append(
            f"| （其余 {len(rest)} 种任务合并） | {rest_runs} | - | - | "
            f"{rest_tok / rest_runs:,.0f} | {_fmt_yuan(rest_tok / rest_runs / 1_000_000 * unit_price)} |"
        )

    usage_cov = (
        data["steps_with_usage"] / data["steps_total"] * 100 if data["steps_total"] else 0
    )
    lines += [
        "",
        "## 四、未计费项（不是零成本，是没有账单）",
        "",
        "| 环节 | 用什么 | 为什么不要钱 |",
        "|---|---|---|",
    ]
    for name, tool, why in UNBILLED:
        lines.append(f"| {name} | {tool} | {why} |")

    usage_cov = (
        data["steps_with_usage"] / data["steps_total"] * 100 if data["steps_total"] else 0
    )
    lines += [
        "",
        f"视觉调用（glm-4v-flash）：**{data['vision_calls']}** 次。"
        "它走的是图像链路，本项目的 usage 只记了文本模型，"
        "所以这部分 token **没有计入上面的统计**——列在这里是为了让它可见，而不是假装没有。",
        "",
        f"usage 覆盖率：{data['steps_with_usage']} / {data['steps_total']} 步"
        f"（{usage_cov:.0f}%）。P2 之前的任务没有回传 usage 字段，"
        "覆盖率不到 100% 时，平均 token 会偏低——这是数据缺口，不是省了钱。",
    ]

    # 每个任务的耗时拆到步：这个数才是"成本"里真正贵的那一项，
    # 所以必须从数据里算，不能凭印象写（第一版我手写了"2~4s/步"）。
    steps_per_task = data["steps"] / total
    sec_per_step = data["avg_elapsed"] / steps_per_task if steps_per_task else 0
    tasks_per_yuan = int(1.0 / max(cost_total / total, 1e-12))

    lines += [
        "",
        "## 五、这套数字意味着什么",
        "",
        f"一个典型任务（{steps_per_task:.1f} 步）大约花 "
        f"{tokens_total / total:,.0f} token、{_fmt_yuan(cost_total / total)}（按 {price_name}），"
        f"耗时 {data['avg_elapsed']:.1f}s（≈{sec_per_step:.1f}s/步）。",
        "",
        f"按这个量级，**1 元钱能跑约 {tasks_per_yuan:,} 个任务**。",
        "",
        "所以浏览器操作 Agent 的成本瓶颈**不在模型调用**，而在浏览器进程的内存占用"
        "（M6 压测实测：3 并发时 201MB → 481MB）和单步延迟（就是上面那个 s/步）。",
        "",
        "> 这是本项目做成本核算最重要的结论：模型便宜到可以忽略，"
        "真正的成本是**机器资源和时间**。所以优化方向是并发与步数，不是换更便宜的模型。",
    ]
    return "\n".join(lines) + "\n"


async def main_async(args) -> int:
    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"--since 格式要是 YYYY-MM-DD，收到 {args.since!r}")
            return 1

    data = await collect(since)
    if data["total"] == 0:
        print("库里没有终态任务，先跑几个任务再来算成本（scripts/eval_p1.py --rounds 1）")
        return 1

    report = build_report(data, args.price, since)
    print(report)

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="成本口径：token / 等价人民币 / 实际支出")
    parser.add_argument("--price", default="flashx", choices=sorted(PRICES), help="折算基准档位")
    parser.add_argument("--since", default="", help="只统计这之后创建的任务（YYYY-MM-DD）")
    parser.add_argument("--out", default="", help="另存一份 markdown 到指定路径")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
