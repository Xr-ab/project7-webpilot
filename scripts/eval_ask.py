"""问答评测 CLI（P4-N4）：金标准 QA 集 → 检索命中率 + 答案正确率。

跑法：
    <venv python> scripts/eval_ask.py             # 全量评测
    <venv python> scripts/eval_ask.py --retrieval-only   # 只测检索，不调 LLM（秒级）

为什么先考核检索、再考核生成（这是本脚本最重要的设计）：
    检索是生成的上限——top-k 里没有正确材料，再强的模型也只能编。
    所以两个指标分开测：
      · 检索命中率 = top-3 里有没有一条含正确答案的片段（衡量 N1/N3）
      · 答案正确率 = 最终回答里有没有正确答案（衡量 N4 的 prompt 和模型）
    分开测才能定位问题出在哪一层，混成一个数字什么也说明不了。

金标准怎么定的：每条 expected 都是用 curl / psql 核实过的页面真实内容，
不是凭印象写的（和 eval_real 同一纪律）。

输出：stdout 表格 + runs/eval_ask_<时间戳>/report.md
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# (编号, 问题, 关键字集合, 说明, 是否免检索判定)
# 检索命中判定：top-3 里存在一条片段，包含 keywords 中任意一个
# 答案命中判定：回答文本里包含 keywords 中任意一个（宽松——模型措辞自由，
#               强求逐字匹配会把"对但换了说法"判错）
# retrieval_na=True：幻觉防御题。它的"正确答案"是拒答，检索片段里当然不含
#   "没有相关信息"这样的字——用检索命中率考核它是错误的度量，只看答案层。
QAS: list[tuple[str, str, list[str], str, bool]] = [
    ("Q1", "价格低于 100 元的商品有哪些？", ["无线鼠标", "USB 集线器"], "测试站列表页：价格筛选结果", False),
    ("Q2", "无线鼠标多少钱？", ["89"], "测试站列表页：单价", False),
    ("Q3", "机械键盘的价格是多少？", ["299"], "测试站列表页：单价（干扰项 89/1299）", False),
    ("Q4", "显示器卖多少钱？", ["1299"], "测试站列表页：单价（最大值）", False),
    ("Q5", "谁说过关于爱的名言？", ["Gide", "Neruda", "Monroe", "Marley"], "quotes /tag/love/：作者", False),
    ("Q6", "关于真理（truth）的名言都有哪些作者？", ["Carlin", "Twain", "Rowling"], "quotes /tag/truth/：作者", False),
    ("Q7", "关于智慧（wisdom）的名言作者是谁？", ["Einstein"], "quotes /tag/wisdom/：作者", False),
    ("Q8", "第一页上第一条名言是谁说的？", ["Einstein"], "quotes 首页：首位作者", False),
    ("Q9", "在商品搜索页搜索 usb 有几条结果？", ["1", "一"], "测试站搜索页：结果条数", False),
    ("Q10", "J.K. Rowling 的名言内容是什么？", ["choices", "abilities", "grow to be", "Dumbledore", "inside your head", "选择"], "quotes：她任一条名言（问题无唯一答案）", False),
    ("Q11", "读书（reading）相关的名言有哪些作者？", ["Seuss", "Nicholson", "Martin", "Lewis"], "quotes /tag/reading/：作者", False),
    ("Q12", "火星上有什么生物？", ["没有相关信息", "没有", "无法"], "幻觉防御：库里没有的内容应拒答", True),
]


def _hit(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


async def run(retrieval_only: bool) -> dict:
    from app.knowledge.qa import ask
    from app.knowledge.service import count_nodes, search

    rows: list[dict] = []
    for code, question, keywords, note, retrieval_na in QAS:
        # ---- 检索层
        hits = await search(question, top_k=3)
        retrieval_hit = None if retrieval_na else any(_hit(h["content"], keywords) for h in hits)
        top_sim = hits[0]["similarity"] if hits else 0.0

        row = {
            "code": code,
            "question": question,
            "note": note,
            "retrieval_hit": retrieval_hit,  # None = 本题不考核检索
            "top_sim": top_sim,
            "top_title": hits[0]["title"] if hits else "",
            "answer": "",
            "answer_hit": None,  # None = 未测
        }

        # ---- 生成层（可选）
        if not retrieval_only:
            try:
                result = await ask(question, top_k=3)
                row["answer"] = result["answer"]
                row["answer_hit"] = _hit(result["answer"], keywords)
            except Exception as exc:
                row["answer"] = f"（问答异常：{exc}）"
                row["answer_hit"] = False

        rows.append(row)
        ret_flag = "-" if retrieval_na else ("✓" if retrieval_hit else "✗")
        extra = "" if retrieval_only else f" | 答案{'✓' if row['answer_hit'] else '✗'}"
        print(f"{code:4} 检索{ret_flag}{extra}  sim={top_sim:.3f}  {question[:40]}")

    return {"rows": rows, "nodes": await count_nodes()}


def render(report: dict, retrieval_only: bool) -> str:
    rows = report["rows"]
    n = len(rows)
    # 检索命中率的分母只含"需要检索"的题（幻觉题不算）
    ret_rows = [r for r in rows if r["retrieval_hit"] is not None]
    ret_ok = sum(1 for r in ret_rows if r["retrieval_hit"])
    lines = [
        "# 问答评测报告（P4-N4）",
        "",
        f"知识库节点：{report['nodes']}",
        f"题库：{n} 条（其中检索考核 {len(ret_rows)} 条、幻觉防御 {n - len(ret_rows)} 条）"
        + ("｜仅检索模式" if retrieval_only else ""),
        "",
        "## 汇总",
        "",
        f"- 检索命中率（top-3 含答案）：**{ret_ok}/{len(ret_rows)} = {ret_ok / len(ret_rows) * 100:.1f}%**",
    ]
    if not retrieval_only:
        ans_ok = sum(1 for r in rows if r["answer_hit"])
        lines.append(f"- 答案正确率：**{ans_ok}/{n} = {ans_ok / n * 100:.1f}%**")
    lines += ["", "## 逐题", "", "| 题 | 问题 | 检索 | 答案 | 相似度 | 说明 |", "|---|---|---|---|---|---|"]
    for r in rows:
        ret = "-" if r["retrieval_hit"] is None else ("✓" if r["retrieval_hit"] else "✗")
        ans = "-" if r["answer_hit"] is None else ("✓" if r["answer_hit"] else "✗")
        lines.append(
            f"| {r['code']} | {r['question']} | {ret} | {ans} | {r['top_sim']:.3f} | {r['note']} |"
        )
    if not retrieval_only:
        lines += ["", "## 回答明细", ""]
        for r in rows:
            lines.append(f"**{r['code']}** {r['question']}\n\n> {r['answer']}\n")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="问答评测")
    parser.add_argument("--retrieval-only", action="store_true", help="只测检索，不调 LLM")
    args = parser.parse_args()

    started = time.time()
    report = await run(args.retrieval_only)
    text = render(report, args.retrieval_only)
    print("\n" + text)

    out_dir = PROJECT_ROOT / "runs" / f"eval_ask_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    print(f"\n报告已写入 {out_dir / 'report.md'}（耗时 {time.time() - started:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
