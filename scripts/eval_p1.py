"""P1 评测脚本：5 个任务 × 3 轮，走完整链路（API → 后台执行 → 落盘 → 取轨迹）。

跑法：
    1) 起后端：cd backend && <venv python> -m uvicorn app.main:app --port 8000
    2) <venv python> scripts/eval_p1.py            # 默认 3 轮
       <venv python> scripts/eval_p1.py --rounds 1 # 快速冒烟

为什么必须带答案校验：
`status=success` 只代表"模型说它做完了"。M5 联调时就出现过一次"状态成功、答案正确、
但操作对象根本不是目标页面"的情况。只看 status 会得出假的成功率。

为什么走 HTTP 而不是直接调 workflow：
评测要测的是"用户实际用的那条路"——后台任务、落盘、轮询、轨迹序列化全在里面。
直接调 workflow 绕过了三层，出问题也不知道是哪层的。

输出：
    runs/eval_<时间戳>/report.md   人看的表格 + 汇总
    runs/eval_<时间戳>/raw.json    原始结果，便于后续改判定规则重算
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
BASE = "http://127.0.0.1:8000"
TESTSITE = f"{BASE}/testsite"

# 脚本直接运行时 sys.path[0] 就是 scripts/，common.py 直接可导；
# 被 eval_holdout 之类 import 时，对方也已把 scripts/ 插进 path
from common import auth_headers, ensure_token, task_payload  # noqa: E402

# 评测账号的 token：main 开头用 ensure_token 填上，_get/_post 靠它自证身份
_TOKEN = ""

# 单任务最长等待（秒）。比后端 task_timeout_seconds 留一点余量
WAIT_LIMIT = 240


# --------------------------------------------------------------------------
# 答案判定：每个任务一个函数，返回 (是否通过, 原因)
# 判定写在这里而不是让模型自评：模型自评和它执行时的判断是同一个脑子，靠不住。
# --------------------------------------------------------------------------
def norm(text: str) -> str:
    """归一化：去空格、转小写。模型有时写「USB 集线器」有时写「usb集线器」。"""
    return re.sub(r"\s+", "", (text or "")).lower()


PRODUCTS = ["无线鼠标", "机械键盘", "显示器", "USB 集线器", "降噪耳机", "笔记本支架"]
CHEAP = ["无线鼠标", "USB 集线器"]          # 价格低于 100 的两个
EXPENSIVE = ["机械键盘", "显示器", "降噪耳机", "笔记本支架"]


def check_t1(answer: str, _trace: list) -> tuple[bool, str]:
    missing = [p for p in PRODUCTS if norm(p) not in norm(answer)]
    return (not missing, f"缺 {missing}" if missing else "")


def check_t2(answer: str, _trace: list) -> tuple[bool, str]:
    text = norm(answer)
    need = ["提交成功", "张三", "zhang@example.com"]
    missing = [n for n in need if norm(n) not in text]
    if not (("福州" in text) or ("fuzhou" in text)):
        missing.append("城市")
    return (not missing, f"缺 {missing}" if missing else "")


def check_t3(answer: str, trace: list) -> tuple[bool, str]:
    """集合必须完全相等：漏一个算错，多一个也算错（多出来说明它没真筛选）。"""
    text = norm(answer)
    missing = [p for p in CHEAP if norm(p) not in text]
    extra = [p for p in EXPENSIVE if norm(p) in text]
    problems = []
    if missing:
        problems.append(f"漏 {missing}")
    if extra:
        problems.append(f"多了 {extra}")
    return (not problems, "；".join(problems))


def check_t4(answer: str, _trace: list) -> tuple[bool, str]:
    """期望 1 条（catalog 里只有「USB 集线器」命中 usb）。"""
    numbers = re.findall(r"\d+", answer or "")
    if not numbers:
        return (False, f"答案里没有数字：{answer!r}")
    return (numbers[0] == "1", "" if numbers[0] == "1" else f"答了 {numbers[0]}，应为 1")


def check_t5(answer: str, _trace: list) -> tuple[bool, str]:
    text = norm(answer)
    ok_name = "机械键盘" in text
    ok_price = "299" in text
    if ok_name and ok_price:
        return (True, "")
    return (False, f"名称对={ok_name} 价格对={ok_price}")


# ---- P7 扩题（决策 1-B：5 题 → 10 题，样本从 15 次提到 30 次）----
# 扩题的选型标准：**每题考一个前面 5 题没覆盖的能力**，不是凑数。
def check_t6(answer: str, _trace: list) -> tuple[bool, str]:
    """T6 读回显（表单只填一项就提交，要求读出"没填的字段是什么样"）。

    考的是"以页面为准"：成功回显里城市显示「未填」、订阅显示「否」。
    模型如果按自己的想象补一句"城市已填福州"，就说明它没读结果、只在复述目标。
    """
    text = norm(answer)
    problems = []
    if "李四" not in text:
        problems.append("姓名没回显")
    if "未填" not in text:
        problems.append("没读出「城市=未填」")
    if "否" not in text:
        problems.append("没读出「订阅=否」")
    return (not problems, "；".join(problems))


def check_t7(answer: str, _trace: list) -> tuple[bool, str]:
    """T7 空态识别（筛选一个不存在的关键词，要求说出页面的"没有匹配"提示）。

    考的是"读不到数据时会不会编"——列表页筛选无结果时会显示 empty-tip，
    模型要是把 6 个商品又背一遍，说明它在拿旧快照糊弄。
    """
    text = norm(answer)
    ok = ("没有匹配" in text) or ("无匹配" in text) or ("没有找到" in text)
    return (ok, "" if ok else f"答案没提到空态提示：{answer[:80]!r}")


def check_t8(answer: str, _trace: list) -> tuple[bool, str]:
    """T8 零结果计数（搜 zzz，期望"共 0 条结果"）。

    和 T4（搜得到、答 1 条）配对：一个考"数对了"，一个考"承认是 0"。
    零结果特别容易出错——模型倾向于把 0 说成 1 或者绕开不答。
    """
    numbers = re.findall(r"\d+", answer or "")
    if not numbers:
        return (False, f"答案里没有数字：{answer!r}")
    return (numbers[0] == "0", "" if numbers[0] == "0" else f"答了 {numbers[0]}，应为 0")


def check_t9(answer: str, _trace: list) -> tuple[bool, str]:
    """T9 边界判断（翻到第二页后，判断"还能不能继续翻"）。

    detail2 页面只有「上一页」，没有「下一页」——正确答案是"没有下一页了"。
    这题考的是**否定判断**：找不到某个东西时，能不能确定地说"它不存在"，
    而不是继续点一个不存在的链接直到步数用光（R2 翻页题就是这么废掉的）。
    """
    text = norm(answer)
    negatives = ["没有", "不能再", "不能继续", "不存在", "已经是最后一页", "最后一页"]
    ok = any(word in text for word in negatives)
    return (ok, "" if ok else f"没有明确给出否定判断：{answer[:80]!r}")


def check_t10(answer: str, _trace: list) -> tuple[bool, str]:
    """T10 聚合比较（6 个商品里找价格最高的）。

    考的是"把所有行都读到再比较"：最高价是显示器 1299。
    和 T3（低于 100 的两个）配对：一个考过滤集合，一个考极值。
    """
    text = norm(answer)
    problems = []
    if "显示器" not in text:
        problems.append("名称不对（应为显示器）")
    if "1299" not in text:
        problems.append("价格不对（应为 1299）")
    return (not problems, "；".join(problems))


TASKS = [
    ("T1", "导航+提取", "从首页进入产品列表，提取所有产品名称", f"{TESTSITE}/index.html", check_t1),
    ("T2", "表单提交",
     "打开资料填写页，姓名填张三、邮箱填 zhang@example.com、城市选福州、勾选订阅，然后提交",
     f"{TESTSITE}/form.html", check_t2),
    ("T3", "读表判断", "在产品列表页找出所有价格低于 100 元的商品名称", f"{TESTSITE}/list.html", check_t3),
    ("T4", "搜索计数", "在商品搜索页搜索 usb，告诉我一共几条结果", f"{TESTSITE}/search.html", check_t4),
    ("T5", "多步翻页", "打开商品详情页，点下一页，告诉我第二页商品的名字和价格", f"{TESTSITE}/detail.html", check_t5),
    # ---- P7 扩题（每题一个新能力点，见上面各判定函数的说明）----
    # T6 题干必须把"姓名"也问上：判定要查姓名回显，题干不问就是判定过严——
    # 首轮试跑就出过这个错（答案「城市=未填，订阅=否」本身完全正确，却被判 fail）。
    ("T6", "读回显", "打开资料填写页，只在姓名里填「李四」，其它都不填，直接提交，"
     "然后告诉我提交回显里的姓名、城市和订阅状态", f"{TESTSITE}/form.html", check_t6),
    ("T7", "空态识别", "打开产品列表页，在筛选框里输入 xyz 然后筛选，告诉我页面显示了什么提示",
     f"{TESTSITE}/list.html", check_t7),
    ("T8", "零结果计数", "在商品搜索页搜索 zzz，告诉我一共有几条结果", f"{TESTSITE}/search.html", check_t8),
    ("T9", "边界判断", "打开商品详情页，点下一页，然后告诉我这一页还能不能继续点下一页",
     f"{TESTSITE}/detail.html", check_t9),
    ("T10", "聚合极值", "在产品列表页找出价格最高的商品，告诉我它的名称和价格",
     f"{TESTSITE}/list.html", check_t10),
]


# --------------------------------------------------------------------------
# HTTP 封装（只用标准库，避免评测脚本再引一个依赖）
# --------------------------------------------------------------------------
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


def run_once(goal: str, start_url: str) -> dict:
    """提交一个任务并等它结束。返回 meta + trace。"""
    task_id = _post(
        "/api/v1/tasks",
        # require_approval 默认 False 已收进 common.task_payload：为什么批处理
        # 不能等审批、漏写会怎样（P5 第十节回归防护漏 4 脚本），见 common.py
        task_payload(goal, start_url),
    )["task_id"]

    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        meta = _get(f"/api/v1/tasks/{task_id}")
        if meta["status"] not in ("queued", "running"):
            trace = _get(f"/api/v1/tasks/{task_id}/trace")["trace"]
            return {"meta": meta, "trace": trace}
        time.sleep(1.0)

    return {
        "meta": {"task_id": task_id, "status": "eval_timeout", "steps": 0, "answer": "", "note": "评测脚本等待超时"},
        "trace": [],
    }


def main(rounds: int, only: list[str] | None = None) -> int:
    global _TOKEN
    try:
        _get("/health")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        print(f"服务没起：{BASE} 打不开。先跑：cd backend && <venv python> -m uvicorn app.main:app --port 8000")
        return 1
    # M4 之后任务接口要 token（health 不要，所以 health 过了不代表能提交任务）
    _TOKEN = ensure_token(BASE)

    # --only 是给"改了一条判定、想只验这一条"用的：跑全量 10 题要几分钟，
    # 调试时等不起。顺带的好处是报告里能写清"这条数字是用哪条命令跑出来的"。
    tasks = TASKS
    if only:
        wanted = {c.strip().upper() for c in only}
        tasks = [t for t in TASKS if t[0] in wanted]
        if not tasks:
            print(f"--only 里没有匹配的任务：{only}；可选 {[t[0] for t in TASKS]}")
            return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT_ROOT / "runs" / f"eval_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    total = len(tasks) * rounds
    done = 0

    for round_no in range(1, rounds + 1):
        for code, name, goal, url, checker in tasks:
            done += 1
            print(f"[{done}/{total}] {code} {name} 第{round_no}轮 … ", end="", flush=True)
            started = time.perf_counter()
            outcome = run_once(goal, url)
            wall = time.perf_counter() - started

            meta = outcome["meta"]
            answer_ok, reason = checker(meta.get("answer", ""), outcome["trace"])
            # 判定口径：状态成功 **且** 答案对，才算这个任务真的成功
            verdict = "pass" if (meta["status"] == "success" and answer_ok) else "fail"

            results.append(
                {
                    "code": code,
                    "name": name,
                    "round": round_no,
                    "task_id": meta.get("task_id"),
                    "status": meta.get("status"),
                    "answer": meta.get("answer", ""),
                    "answer_ok": answer_ok,
                    "answer_reason": reason,
                    "steps": meta.get("steps", 0),
                    "elapsed_s": meta.get("elapsed_s"),
                    "wall_s": round(wall, 2),
                    # note 一定要留：状态不是 success 时，它是唯一能区分
                    # "模型跑错" 和 "环境出事（磁盘/端口/浏览器崩了）" 的线索。
                    # 第一版没记，结果一条 WinError 5 的落盘错误被算进了能力失败。
                    "note": meta.get("note", ""),
                    "verdict": verdict,
                }
            )
            print(f"{verdict}  状态={meta.get('status')}  步数={meta.get('steps')}  答案={meta.get('answer', '')[:40]!r}")
            if verdict == "fail":
                print(f"        判定原因：{reason or '状态未成功'}")
                if meta.get("note"):
                    print(f"        后端说明：{str(meta['note'])[:160]}")

    # ---- 汇总 ----
    passed = [r for r in results if r["verdict"] == "pass"]
    rate = len(passed) / len(results) * 100

    by_code: dict[str, list[dict]] = {}
    for item in results:
        by_code.setdefault(item["code"], []).append(item)

    fail_attr: dict[str, int] = {}
    for item in results:
        if item["verdict"] == "pass":
            continue
        if item["status"] != "success":
            key = item["status"]
        else:
            key = "答案错误"          # 状态成功但答案不对，单独一类，最容易骗人的那种
        fail_attr[key] = fail_attr.get(key, 0) + 1

    avg_steps = sum(r["steps"] for r in passed) / len(passed) if passed else 0
    avg_time = sum((r["elapsed_s"] or 0) for r in passed) / len(passed) if passed else 0

    # ---- 报告 ----
    lines = [
        "# P1 评测报告",
        "",
        f"- 时间：{stamp}",
        f"- 样本：{len(tasks)} 任务 × {rounds} 轮 = {len(results)} 次",
        f"- 判定口径：`status=success` **且** 答案校验通过，才算成功",
        "",
        f"## 总成功率：{len(passed)}/{len(results)} = {rate:.1f}%",
        "",
        f"- 成功任务平均步数：{avg_steps:.1f}",
        f"- 成功任务平均耗时：{avg_time:.1f}s",
        "",
        "## 分任务",
        "",
        "| 任务 | 通过 | 成功率 | 平均步数 | 平均耗时 |",
        "|---|---|---|---|---|",
    ]
    for code, name, *_ in tasks:
        items = by_code.get(code, [])
        ok = [i for i in items if i["verdict"] == "pass"]
        rate_i = len(ok) / len(items) * 100 if items else 0
        steps_i = sum(i["steps"] for i in ok) / len(ok) if ok else 0
        time_i = sum((i["elapsed_s"] or 0) for i in ok) / len(ok) if ok else 0
        lines.append(f"| {code} {name} | {len(ok)}/{len(items)} | {rate_i:.0f}% | {steps_i:.1f} | {time_i:.1f}s |")

    lines += ["", "## 失败归因", ""]
    if fail_attr:
        lines += ["| 归因 | 次数 |", "|---|---|"]
        for key, count in sorted(fail_attr.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {count} |")
    else:
        lines.append("（无失败）")

    lines += ["", "## 逐次明细", "", "| 任务 | 轮 | 判定 | 状态 | 步数 | 耗时 | 答案 |", "|---|---|---|---|---|---|---|"]
    for item in results:
        answer = (item["answer"] or "").replace("|", "/")[:46]
        lines.append(
            f"| {item['code']} | {item['round']} | {item['verdict']} | {item['status']} | "
            f"{item['steps']} | {item['elapsed_s']}s | {answer} |"
        )

    # 失败明细单独一节：失败原因和"后端自己怎么说"要并排放，
    # 否则一条环境错误（磁盘/端口）会被当成模型能力问题写进报告。
    failed = [r for r in results if r["verdict"] == "fail"]
    if failed:
        lines += ["", "## 失败明细（判定原因 + 后端说明）", ""]
        for r in failed:
            lines.append(f"- **{r['code']} 第{r['round']}轮**（{r['status']}，{r['steps']} 步）")
            if r.get("answer_reason"):
                lines.append(f"  - 判定：{r['answer_reason']}")
            if r.get("answer"):
                lines.append(f"  - 答案：{r['answer'][:120]}")
            if r.get("note"):
                lines.append(f"  - 后端说明：{str(r['note'])[:200]}")
            if r.get("task_id"):
                lines.append(f"  - task_id：`{r['task_id']}`")

    report = "\n".join(lines) + "\n"
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "raw.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(report)
    print(f"报告：{out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--only", default="", help="只跑指定的任务码，逗号分隔，如 T8,T9")
    args = parser.parse_args()
    picked = [c for c in args.only.split(",") if c.strip()]
    raise SystemExit(main(max(1, args.rounds), picked or None))
