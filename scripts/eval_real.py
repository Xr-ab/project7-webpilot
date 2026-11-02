"""真实站点评测（P2.5 阶段 0）：先摸黑，再决定改什么。

设计意图——**这个脚本不改任何业务代码，只负责把失败摊开**。

为什么要单独一个脚本，不直接扩 eval_p1.py：
- 测试站的题判定的是"能力"（会不会筛选、会不会翻页），真实站点的题判定的是"环境适配"
  （能不能等到渲染、元素被截断时还能不能找到目标）。两者的失败归因完全不同，
  混在一份报告里会看糊。
- 测试站是可控的，真实站点会变。所以这里的数字只能看"当下快照"，不能当长期基线。

和 eval_p1.py 一致的两条原则：
1. **必须校验答案**：`status=success` 只代表模型说它做完了。
2. **走完整 HTTP 链路**：提交 → 后台执行 → 落盘 → 拿轨迹，测用户真走的那条路。

跑法：
    1) 起后端（白名单要已在 .env 里加上目标 host）
    2) <venv python> scripts/eval_real.py --rounds 2
       <venv python> scripts/eval_real.py --only R1 R3   # 只跑指定几题

输出：
    runs/eval_real_<时间戳>/report.md   人看的表格 + 失败机制归因
    runs/eval_real_<时间戳>/raw.json    原始结果 + 每次的轨迹诊断
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"

from common import task_payload  # noqa: E402  脚本直跑时 sys.path[0] 就是 scripts/

# 真实站点慢，等待上限比 eval_p1 的 240s 放宽。比后端 TASK_TIMEOUT_SECONDS 留余量。
WAIT_LIMIT = 300

# M4 之后任务接口默认要求登录（AUTH_ENABLED=true）。脚本用一个固定评测账号：
# 先注册（已存在会 409）再登录，拿到 token 后所有请求带上。
# 为什么不在 .env 里关认证跑评测：那等于用"和生产不同的配置"测，测出来的东西不算数。
_EVAL_EMAIL = "eval@webpilot.dev"
_EVAL_PASSWORD = "eval-real-2026"
_TOKEN = ""


# --------------------------------------------------------------------------
# 答案判定
# 标准答案是**先用 curl 抓页面确认过的**，不是凭印象写的。
# 真实站点改版会让这些答案过期——过期了就重新抓一遍，别先怀疑模型。
#
# 抓取时间：2026-10-23
#
# 为什么最终只用 quotes.toscrape 和 MDN（这本身是阶段 0 的第一条产出）：
# 原本还选了 books.toscrape.com，实测它在**浏览器里冷启动要 43.7 秒**
# （同一个浏览器进程里第二次 0.0s，说明是连接建立慢，不是页面大）。
# 我们的架构是每任务一个新浏览器（ADR-002），等于每题白烧 44 秒，
# 评测结果会被网络噪声淹没，测不出 Agent 的真实水平。
# 同批实测：quotes 1.5s、MDN 0.8s、baidu 0.7s。
# 结论：评测站点的第一筛选条件不是"真实度"，而是"网络可复现"。
# --------------------------------------------------------------------------
def norm(text: str) -> str:
    """归一化：去空白、去标点、转小写。

    真实站点的文本里会有 &amp; &#39; 这类实体、还有各种引号，
    不归一化的话"对"的答案也会被判错。
    """
    text = (text or "").replace("&#39;", "'").replace("&amp;", "&")
    text = re.sub(r"[\s\u00a0]+", "", text)
    text = re.sub(r"[，,。.、:：;；\"'“”‘’()（）]", "", text)
    return text.lower()


def check_r1(answer: str, _trace: list) -> tuple[bool, str]:
    """quotes 首页第一条名言的作者。

    标准答案：Albert Einstein
    """
    ok = "einstein" in norm(answer)
    return (ok, "" if ok else f"答案是 {answer!r}，应为 Albert Einstein")


def check_r2(answer: str, _trace: list) -> tuple[bool, str]:
    """quotes 翻到第 2 页，第一条名言的作者。

    标准答案：Marilyn Monroe
    注意：第 1 页第一条也是 Einstein，所以答 Einstein 说明**根本没翻页**——
    这题就是用来抓"没翻页却说翻完了"的。
    """
    text = norm(answer)
    if "monroe" in text:
        return (True, "")
    if "einstein" in text:
        return (False, "答的仍是第 1 页的作者（Albert Einstein），说明没有真的翻页")
    return (False, f"答案是 {answer!r}，应为 Marilyn Monroe")


def check_r3(answer: str, _trace: list) -> tuple[bool, str]:
    """quotes 点标签 love，第一条名言的作者。

    标准答案：André Gide
    """
    text = norm(answer)
    ok = "gide" in text
    return (ok, "" if ok else f"答案是 {answer!r}，应为 André Gide")


def check_r4(answer: str, _trace: list) -> tuple[bool, str]:
    """MDN 搜索 Array.prototype.map，说明返回值。

    标准答案：一个新数组（a new array），元素是回调函数的返回值
    这题的核心不是答案，而是**能不能在纯前端渲染的搜索页上拿到内容**。
    """
    text = norm(answer)
    ok = ("新数组" in text) or ("newarray" in text) or ("array" in text)
    return (ok, "" if ok else f"答案没提到数组：{answer!r}")


# 题号, 名称, 目标, 起点, 判定函数, 这道题在考察什么
TASKS = [
    (
        "R1", "静态提取",
        "这个页面上第一条名言是谁说的？",
        "https://quotes.toscrape.com/",
        check_r1, "基础：真实 HTTP + 外部 CSS，能否正确读列表",
    ),
    (
        "R2", "翻页",
        "点击下一页，告诉我新一页上第一条名言是谁说的",
        "https://quotes.toscrape.com/",
        check_r2, "中等：点 Next 后要重新读快照（编号会重排）",
    ),
    (
        "R3", "标签导航",
        "点击标签 love，告诉我这个标签下第一条名言是谁说的",
        "https://quotes.toscrape.com/",
        check_r3, "中等：侧栏几十个同名风格的标签链接，易点错",
    ),
    (
        "R4", "前端渲染 + 搜索",
        "在搜索框里搜索 Array.prototype.map，打开结果，告诉我这个方法的返回值是什么",
        "https://developer.mozilla.org/en-US/",
        check_r4, "难点：首页 169 个链接（超上限 80）+ 搜索页纯前端渲染",
    ),
]


# --------------------------------------------------------------------------
# 失败机制归因
# 这部分是本脚本的核心价值：光知道"失败了"没用，要知道**死在哪一层**。
# --------------------------------------------------------------------------
def diagnose(meta: dict, trace: list[dict]) -> list[str]:
    """从轨迹里推断失败机制。返回若干标签（可能多个同时成立）。

    为什么不能只看 status：
    status 只有 success / step_limit / loop / timeout 这几种粗分类。
    同样一个 step_limit，可能是"页面内容太多找不到目标"，也可能是"加载没完成一直空转"。
    这两种的修法完全相反（一个是缩小快照，一个是等更久）。
    """
    tags: list[str] = []
    status = meta.get("status")

    # ---- 快照层面的机制 ----
    totals = [int(t.get("elements_total") or 0) for t in trace]
    first = totals[0] if totals else 0
    if totals and first == 0:
        # 第一步就 0 个可交互元素：几乎可以断定是"页面还没渲染就取快照"
        tags.append("首屏快照为空（加载等待不足）")
    if totals and all(t == 80 for t in totals if t > 0):
        # 每步都刚好顶到上限，说明真实元素数远超容量，目标很可能被截断掉了
        tags.append("元素数顶到上限 80（目标可能被截断）")
    if totals and max(totals) > 60:
        tags.append("元素密集（>60，接近上限）")

    # ---- 动作层面的机制 ----
    errs = " ".join(str(t.get("error") or "") for t in trace).lower()
    if "intercept" in errs:
        tags.append("点击被遮挡（弹窗/遮罩）")
    if "timeout" in errs and "waiting for" in errs:
        tags.append("等待元素出现超时（未渲染或选择器失效）")
    if "not visible" in errs:
        tags.append("目标元素不可见")

    # ---- 状态层面的归因 ----
    if status == "step_limit":
        tags.append("步数用尽（MAX_STEPS=12，真实站点层级更深）")
    elif status == "timeout":
        tags.append("任务超时（TASK_TIMEOUT=180s）")
    elif status == "loop":
        tags.append("原地打转（同一动作重复）")
    elif status == "invalid_action":
        tags.append("动作非法（编号不存在，多为模型编编号）")
    elif status == "nav_failed":
        tags.append("页面打不开（网络/白名单）")
    elif status == "error":
        tags.append("内部错误")

    if status == "success" and not tags:
        tags.append("状态成功但答案错（模型自认为做完了）")

    return tags or ["未识别"]


# --------------------------------------------------------------------------
# HTTP 封装（复用 eval_p1 的写法，只用标准库）
# --------------------------------------------------------------------------
def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login() -> str:
    """拿 token：先注册（已存在 409）→ 登录；认证关闭（403）时返回空串。"""
    creds = {"email": _EVAL_EMAIL, "password": _EVAL_PASSWORD}
    try:
        return _post("/api/v1/auth/register", creds)["token"]
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return _post("/api/v1/auth/login", creds)["token"]
        if exc.code == 403:
            return ""
        raise


def run_once(goal: str, start_url: str) -> dict:
    # require_approval 默认 False 收进了 common.task_payload（批处理不能等审批，
    # P5a 教训见 common.py）
    task_id = _post("/api/v1/tasks", task_payload(goal, start_url))["task_id"]

    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        meta = _get(f"/api/v1/tasks/{task_id}")
        if meta["status"] not in ("queued", "running"):
            trace = _get(f"/api/v1/tasks/{task_id}/trace")["trace"]
            return {"meta": meta, "trace": trace}
        time.sleep(1.5)

    return {
        "meta": {"task_id": task_id, "status": "eval_timeout", "steps": 0, "answer": "", "note": "评测脚本等待超时"},
        "trace": [],
    }


def main(rounds: int, only: list[str] | None) -> int:
    global _TOKEN
    try:
        _get("/health")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        print(f"服务没起：{BASE} 打不开。先跑：cd backend && <venv python> -m uvicorn app.main:app --port 8000")
        return 1

    # /health 不需要认证，但 /api/v1/tasks 需要：先拿 token 再开始
    try:
        _TOKEN = login()
        print(f"认证：{'已登录' if _TOKEN else '已关闭（AUTH_ENABLED=false）'}")
    except urllib.error.HTTPError as exc:
        print(f"登录失败（HTTP {exc.code}）：{exc.read().decode('utf-8', 'ignore')[:200]}")
        return 1

    tasks = [t for t in TASKS if not only or t[0] in only]
    if not tasks:
        print(f"没有匹配的任务，可选：{[t[0] for t in TASKS]}")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT_ROOT / "runs" / f"eval_real_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    total = len(tasks) * rounds
    done = 0

    for round_no in range(1, rounds + 1):
        for code, name, goal, url, checker, probe in tasks:
            done += 1
            print(f"[{done}/{total}] {code} {name} 第{round_no}轮 … ", end="", flush=True)
            started = time.perf_counter()
            outcome = run_once(goal, url)
            wall = time.perf_counter() - started

            meta = outcome["meta"]
            answer_ok, reason = checker(meta.get("answer", ""), outcome["trace"])
            verdict = "pass" if (meta["status"] == "success" and answer_ok) else "fail"
            tags = [] if verdict == "pass" else diagnose(meta, outcome["trace"])

            results.append(
                {
                    "code": code,
                    "name": name,
                    "probe": probe,
                    "round": round_no,
                    "task_id": meta.get("task_id"),
                    "status": meta.get("status"),
                    "answer": meta.get("answer", ""),
                    "answer_ok": answer_ok,
                    "answer_reason": reason,
                    "steps": meta.get("steps", 0),
                    "elapsed_s": meta.get("elapsed_s"),
                    "wall_s": round(wall, 2),
                    "verdict": verdict,
                    "mechanisms": tags,
                    "elements_per_step": [t.get("elements_total") for t in outcome["trace"]],
                    "errors": [t.get("error") for t in outcome["trace"] if t.get("error")],
                }
            )

            print(f"{verdict}  状态={meta.get('status')}  步数={meta.get('steps')}  答案={(meta.get('answer') or '')[:36]!r}")
            if verdict == "fail":
                print(f"        判定：{reason or '状态未成功'}")
                print(f"        机制：{' / '.join(tags)}")

    # ---- 汇总 ----
    passed = [r for r in results if r["verdict"] == "pass"]
    rate = len(passed) / len(results) * 100

    mech_count: dict[str, int] = {}
    for item in results:
        if item["verdict"] == "pass":
            continue
        for tag in item["mechanisms"]:
            mech_count[tag] = mech_count.get(tag, 0) + 1

    by_code: dict[str, list[dict]] = {}
    for item in results:
        by_code.setdefault(item["code"], []).append(item)

    lines = [
        "# 真实站点评测报告（阶段 0：只测不改）",
        "",
        f"- 时间：{stamp}",
        f"- 样本：{len(tasks)} 任务 × {rounds} 轮 = {len(results)} 次",
        "- 判定口径：`status=success` **且** 答案校验通过，才算成功",
        "- 站点：quotes.toscrape.com / developer.mozilla.org",
        "- 注：books.toscrape.com 因浏览器侧冷启动 43.7s 被剔除（见脚本头注释）",
        "",
        f"## 总成功率：{len(passed)}/{len(results)} = {rate:.1f}%",
        "",
        "## 分任务",
        "",
        "| 题 | 名称 | 考察点 | 通过 | 平均步数 | 平均耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for code, name, _goal, _url, _checker, probe in tasks:
        items = by_code.get(code, [])
        ok = [i for i in items if i["verdict"] == "pass"]
        steps_i = sum(i["steps"] for i in ok) / len(ok) if ok else 0
        time_i = sum((i["elapsed_s"] or 0) for i in ok) / len(ok) if ok else 0
        lines.append(
            f"| {code} | {name} | {probe} | {len(ok)}/{len(items)} | "
            f"{steps_i:.1f} | {time_i:.1f}s |"
        )

    lines += ["", "## 失败机制归因（同一题可能有多个机制）", ""]
    if mech_count:
        lines += ["| 机制 | 出现次数 |", "|---|---|"]
        for key, count in sorted(mech_count.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {count} |")
    else:
        lines.append("（无失败）")

    lines += ["", "## 逐次明细", ""]
    for item in results:
        answer = (item["answer"] or "").replace("|", "/")[:40]
        lines.append(f"### {item['code']} 第{item['round']}轮 — {item['verdict']}")
        lines.append("")
        lines.append(f"- 状态：{item['status']}　步数：{item['steps']}　耗时：{item['elapsed_s']}s　任务：{item['task_id']}")
        lines.append(f"- 答案：{answer!r}")
        if item["verdict"] == "fail":
            lines.append(f"- 判定原因：{item['answer_reason'] or '状态未成功'}")
            lines.append(f"- 失败机制：{' / '.join(item['mechanisms'])}")
            lines.append(f"- 每步可交互元素数：{item['elements_per_step']}")
            if item["errors"]:
                lines.append(f"- 动作错误：{item['errors'][:3]}")
        lines.append("")

    report = "\n".join(lines) + "\n"
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "raw.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(report)
    print(f"报告：{out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--only", nargs="*", help="只跑指定题号，例如 --only R1 R3")
    args = parser.parse_args()
    raise SystemExit(main(max(1, args.rounds), args.only))
