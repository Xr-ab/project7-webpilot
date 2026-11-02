"""泛化探针：三个**故意不在 few-shot 示例里**的任务。

存在的理由：
M6 正式评测（eval_p1.py）跑出 100%，但这个数字没有说服力——T1-T5 和提示词里的
3 个示例高度重合（T2 像示例 1、T3 像示例 2、T4 像示例 3）。那更像"背下来了"，
而不是"会了"。

所以单独做这组探针，测的是**分布外**表现：
- 两步子任务（先搜 A 记数，再搜 B 记数）
- 只填一部分、明确禁止提交
- 需要比较全部数值的推理（页面上没有排序功能）

跑法：
    <venv python> scripts/eval_probe.py
"""
from __future__ import annotations

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
WAIT_LIMIT = 240

from common import auth_headers, ensure_token, task_payload  # noqa: E402  脚本直跑时 sys.path[0] 就是 scripts/


def norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def check_sort(answer: str, _trace: list) -> tuple[bool, str]:
    """最贵的是「显示器 ¥1299」。"""
    text = norm(answer)
    ok = ("显示器" in text) and ("1299" in text)
    return (ok, "" if ok else f"期望 显示器/1299，实际 {answer!r}")


def check_partial_fill(answer: str, _trace: list) -> tuple[bool, str]:
    """只填姓名不提交：答案要体现「李四」且**不能**出现提交成功。"""
    text = norm(answer)
    if "提交成功" in text:
        return (False, "违反指令：任务明确要求不要提交，它提交了")
    ok = "李四" in text
    return (ok, "" if ok else f"期望体现 李四，实际 {answer!r}")


def check_two_searches(answer: str, _trace: list) -> tuple[bool, str]:
    """键盘→1 条、鼠标→1 条，两个都要报。只报一个 = 没完成。"""
    text = norm(answer)
    has_keyword = "键盘" in text
    has_mouse = "鼠标" in text
    numbers = re.findall(r"\d+", answer or "")
    if not (has_keyword and has_mouse):
        return (False, f"只报了其中一次搜索：{answer!r}")
    if len(numbers) < 2:
        return (False, f"两个条数没都给出：{answer!r}")
    return (True, "")


PROBES = [
    ("P1 排序推理", "把产品列表里的商品按价格从低到高排一下，告诉我最贵的是哪个、多少钱",
     f"{TESTSITE}/list.html", check_sort),
    ("P2 部分填写不提交", "打开资料填写页，只在姓名里填李四，其他都别动，不要提交；然后告诉我姓名的值是什么",
     f"{TESTSITE}/form.html", check_partial_fill),
    ("P3 连续两次搜索", "在商品搜索页先搜 键盘 记下条数，再搜 鼠标 记下条数，分别告诉我",
     f"{TESTSITE}/search.html", check_two_searches),
]


from common import auth_headers, ensure_token, task_payload  # noqa: E402

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
            trace = _get(f"/api/v1/tasks/{task_id}/trace")["trace"]
            return {"meta": meta, "trace": trace}
        time.sleep(1.0)
    return {"meta": {"status": "probe_timeout", "answer": "", "steps": 0, "elapsed_s": None}, "trace": []}


def main() -> int:
    global _TOKEN
    try:
        _get("/health")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        print(f"服务没起：{BASE} 打不开")
        return 1
    _TOKEN = ensure_token(BASE)  # 任务接口要 token（health 不要，过了不代表能提交）

    passed = 0
    results = []
    for name, goal, url, checker in PROBES:
        print(f"{name} … ", end="", flush=True)
        outcome = run_once(goal, url)
        meta = outcome["meta"]
        ok, reason = checker(meta.get("answer", ""), outcome["trace"])
        ok = ok and meta["status"] == "success"
        passed += 1 if ok else 0
        verdict = "pass" if ok else "fail"
        print(f"{verdict}  步数={meta.get('steps')}  答案={meta.get('answer', '')[:60]!r}")
        if not ok:
            detail = reason or f"状态={meta.get('status')}"
            print(f"        {detail}")

        results.append(
            {
                "probe": name,
                "goal": goal,
                "status": meta.get("status"),
                "steps": meta.get("steps"),
                "elapsed_s": meta.get("elapsed_s"),
                "answer": meta.get("answer", ""),
                "verdict": verdict,
                "reason": reason,
                "trace": [
                    {"step": t["step"], "action": t["action"], "thought": t.get("thought", "")}
                    for t in outcome["trace"]
                ],
            }
        )

    print(f"\n泛化通过率：{passed}/{len(PROBES)} = {passed / len(PROBES) * 100:.0f}%")

    out_dir = PROJECT_ROOT / "runs" / f"probe_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细：{out_dir / 'raw.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
