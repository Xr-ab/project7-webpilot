"""隔离评测集（holdout）：10 个任务，**故意不进 few-shot 示例**。

为什么单独一份：
eval_p1.py 的 T1-T5 与提示词的 3 个示例同源（T2≈示例1、T3≈示例2、T4≈示例3），
跑出 100% 只能说明"它记住示例了"。这份 holdout 用来量**泛化**：
同一套 Agent、同样的提示词，在没见过的说法上能到多少。

选任务的三个约束：
1. 都能在现有 6 个测试页上客观判定（不需要额外的正确性判断）
2. 覆盖不同能力：计数、去重、筛选、读单元格、往返导航、部分填写、多步骤
3. 说法规避示例里的措辞（不说"提取所有…"、不说"搜 usb 几条"）

跑法：
    <venv python> scripts/eval_holdout.py --rounds 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from eval_p1 import norm  # 复用同一套归一化，避免两份判定口径漂移
from common import auth_headers, ensure_token, task_payload

BASE = "http://127.0.0.1:8000"
TESTSITE = f"{BASE}/testsite"
WAIT_LIMIT = 240


# --------------------------------------------------------------------------
# 判定函数：一律"客观可判"，不写"看起来对"这种模糊标准
# --------------------------------------------------------------------------
def only_number_is(answer: str, expected: str) -> tuple[bool, str]:
    numbers = re.findall(r"\d+", answer or "")
    if not numbers:
        return (False, f"答案里没有数字：{answer!r}")
    ok = numbers[0] == expected
    return (ok, "" if ok else f"答了 {numbers[0]}，应为 {expected}")


def has_all(answer: str, items: list[str]) -> tuple[bool, str]:
    text = norm(answer)
    missing = [i for i in items if norm(i) not in text]
    return (not missing, f"缺 {missing}" if missing else "")


def has_none(answer: str, items: list[str]) -> tuple[bool, str]:
    text = norm(answer)
    extra = [i for i in items if norm(i) in text]
    return (not extra, f"不该出现却出现了 {extra}" if extra else "")


def both(a: tuple[bool, str], b: tuple[bool, str]) -> tuple[bool, str]:
    """两个判定都要过，把两边的失败原因拼起来。"""
    if not a[0]:
        return a
    return b


def check_h1(answer: str, _t: list) -> tuple[bool, str]:
    """城市下拉：请选择 / 福州 / 厦门 / 杭州 = 4 个选项。"""
    return only_number_is(answer, "4")


def check_h2(answer: str, _t: list) -> tuple[bool, str]:
    """详情页「分类」= 外设。"""
    return has_all(answer, ["外设"])


def check_h3(answer: str, _t: list) -> tuple[bool, str]:
    """分类去重：外设 / 显示 / 配件 / 音频 = 4 种。"""
    return only_number_is(answer, "4")


def check_h4(answer: str, _t: list) -> tuple[bool, str]:
    """筛选「配件」后只剩 USB 集线器、笔记本支架。"""
    return both(
        has_all(answer, ["USB 集线器", "笔记本支架"]),
        has_none(answer, ["无线鼠标", "机械键盘", "显示器", "降噪耳机"]),
    )


def check_h5(answer: str, _t: list) -> tuple[bool, str]:
    """搜「显示器」只命中显示器本身。"""
    return both(
        has_all(answer, ["显示器"]),
        has_none(answer, ["无线鼠标", "机械键盘", "降噪耳机", "USB 集线器", "笔记本支架"]),
    )


def check_h6(answer: str, _t: list) -> tuple[bool, str]:
    """下一页再上一页，回到「商品详情：无线鼠标」。"""
    return has_all(answer, ["无线鼠标"])


def check_h7(answer: str, _t: list) -> tuple[bool, str]:
    """城市选杭州后，选中项应为杭州（不能报成 fuzhou 或「请选择」）。"""
    return both(has_all(answer, ["杭州"]), has_none(answer, ["请选择"]))


def check_h8(answer: str, _t: list) -> tuple[bool, str]:
    """搜「耳机」命中降噪耳机，1 条。"""
    return only_number_is(answer, "1")


def check_h9(answer: str, _t: list) -> tuple[bool, str]:
    """USB 集线器价格 59。"""
    text = norm(answer)
    ok = "59" in text
    return (ok, "" if ok else f"期望 59，实际 {answer!r}")


def check_h10(answer: str, _t: list) -> tuple[bool, str]:
    """首页 4 个导航入口。"""
    return only_number_is(answer, "4")


TASKS = [
    ("H1", "下拉选项计数", "在资料填写页，告诉我「城市」下拉框里一共有几个选项", f"{TESTSITE}/form.html", check_h1),
    ("H2", "读单元格", "打开商品详情页，告诉我「分类」是什么", f"{TESTSITE}/detail.html", check_h2),
    ("H3", "分类去重", "在产品列表页，告诉我一共有几种不同的分类", f"{TESTSITE}/list.html", check_h3),
    ("H4", "筛选后取集合", "在产品列表页筛选「配件」，告诉我剩下哪些商品", f"{TESTSITE}/list.html", check_h4),
    ("H5", "搜索取结果", "在商品搜索页搜索「显示器」，告诉我结果的商品名", f"{TESTSITE}/search.html", check_h5),
    ("H6", "往返导航", "从详情页点下一页，再点上一页，告诉我现在页面标题是什么", f"{TESTSITE}/detail.html", check_h6),
    ("H7", "部分填写", "把城市选成杭州，然后告诉我下拉框里选中的是什么", f"{TESTSITE}/form.html", check_h7),
    ("H8", "搜索计数", "在商品搜索页搜索「耳机」，告诉我有几条结果", f"{TESTSITE}/search.html", check_h8),
    ("H9", "读价格", "在产品列表页告诉我「USB 集线器」的价格", f"{TESTSITE}/list.html", check_h9),
    ("H10", "计数", "首页一共有几个可以点的导航入口", f"{TESTSITE}/index.html", check_h10),
]


_TOKEN = ""  # main 开头 ensure_token 填上


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers=auth_headers(_TOKEN))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **auth_headers(_TOKEN)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_once(goal: str, start_url: str) -> dict:
    task_id = _post("/api/v1/tasks", task_payload(goal, start_url))["task_id"]
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        meta = _get(f"/api/v1/tasks/{task_id}")
        if meta["status"] not in ("queued", "running"):
            return {"meta": meta, "trace": _get(f"/api/v1/tasks/{task_id}/trace")["trace"]}
        time.sleep(1.0)
    return {"meta": {"status": "eval_timeout", "answer": "", "steps": 0, "elapsed_s": None}, "trace": []}


def main(rounds: int, label: str) -> int:
    global _TOKEN
    try:
        _get("/health")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        print(f"服务没起：{BASE} 打不开")
        return 1
    _TOKEN = ensure_token(BASE)  # 任务接口要 token（health 不要，过了不代表能提交）

    results = []
    total = len(TASKS) * rounds
    done = 0

    for round_no in range(1, rounds + 1):
        for code, name, goal, url, checker in TASKS:
            done += 1
            print(f"[{done}/{total}] {code} {name} … ", end="", flush=True)
            outcome = run_once(goal, url)
            meta = outcome["meta"]
            answer_ok, reason = checker(meta.get("answer", ""), outcome["trace"])
            ok = meta["status"] == "success" and answer_ok
            print(f"{'pass' if ok else 'fail'}  步数={meta.get('steps')}  {meta.get('answer', '')[:46]!r}")

            results.append(
                {
                    "code": code, "name": name, "round": round_no,
                    "status": meta.get("status"), "steps": meta.get("steps"),
                    "elapsed_s": meta.get("elapsed_s"), "answer": meta.get("answer", ""),
                    "answer_ok": answer_ok, "reason": reason,
                    "verdict": "pass" if ok else "fail",
                }
            )

    passed = [r for r in results if r["verdict"] == "pass"]
    rate = len(passed) / len(results) * 100

    print("\n" + "=" * 66)
    print(f"隔离集（{label}）：{len(passed)}/{len(results)} = {rate:.0f}%")
    for item in results:
        if item["verdict"] == "fail":
            print(f"  ✗ {item['code']} {item['name']}  状态={item['status']}  原因={item['reason'] or '答案不对'}")

    out_dir = PROJECT_ROOT / "runs" / f"holdout_{label}_{time.strftime('%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(
        json.dumps({"label": label, "rate": rate, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"明细：{out_dir / 'raw.json'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--label", default="baseline", help="这次跑的标签，写进产物目录名")
    args = parser.parse_args()
    sys.exit(main(max(1, args.rounds), args.label))
