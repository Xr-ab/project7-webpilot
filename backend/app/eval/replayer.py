"""M5a 回放器：把"历史轨迹"变成"prompt 的模拟器"。

它是什么：
    拿一条历史任务的 trace，把其中每一步"当时问模型的问题"原样重建出来，
    用**现在的代码 / 现在的 prompt** 重新问一遍模型，然后对比两次决策。
    老的决策来自 trace 里冻结的 decision 字段（workflow._record 写入），
    新的决策来自现场调用 LLM。

为什么它成立（核心原理——历史即模拟器）：
    一次决策只依赖三样东西：当时的页面快照 + 任务目标 + 之前的执行历史。
    这三样都被 decision 字段冻结了，所以"不启浏览器、不导航"也能完整还原
    当时的提问。这让我们可以在几秒内回答一个平时要跑几分钟真实任务才能
    回答的问题——"我改了 prompt 之后，同一步模型会做出不同的（更好的）决定吗？"

verdict 分类（对比的结论，共 8 种 + 2 个状态）：
    好事（prompt 改进起了作用）：
        better_earlier_finish  原来绕了弯路，现在一步就能 finish 给出答案
        fixed_rejection        原来这一步被判非法，现在输出合法动作了
    坏事（prompt 改动引入了退化）：
        worse_later_finish     原来一步 finish，现在反而去做多余的动作
        new_invalid            原来是合法的，现在反而被判非法
    待人工核对（不同但不定好坏，判断权在人）：
        different_target       同样动作，点了不同编号（可能是对的也可能是错的）
        different_action       做了不同类型的动作
        different_answer       都 finish 了，但答案不同
        missing_old_decision   老决策不存在（decision.payload 为空）
        replay_error           回放自身出错（网络 / 代码 bug），结论不可信
    状态（不算差异）：
        same                   两次决策语义一致
        skipped                没有冻结输入（M5a 之前的老任务），跳过不评
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any

from app.agent.actions import ActionError, check_action, parse_raw
from app.agent.llm import LLMClient, LLMFormatError
from app.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.browser.snapshot import PageSnapshot
from app.config import settings
from app.storage.files import load_meta, load_trace

# 三组 verdict：回放报告按组给结论，具体每条留给人工看
GOOD_VERDICTS = {"better_earlier_finish", "fixed_rejection"}
BAD_VERDICTS = {"worse_later_finish", "new_invalid"}
REVIEW_VERDICTS = {
    "different_target",
    "different_action",
    "different_answer",
    "missing_old_decision",
    "replay_error",
}

# 答案相似度达到多少算"同一个答案"。
# 0.8 与其说是精确阈值，不如说是"抓明显不同"：模型同一句话换个标点、
# 调个语序都在 0.9+，真正的分歧（不同的人名 / 不同的数字）会掉到 0.5 以下。
ANSWER_SIMILARITY_THRESHOLD = 0.8


def _norm(text: str) -> str:
    """答案归一化：去空白、去标点、转小写。

    口径必须与 scripts/eval_real.py 的 norm 完全一致——
    两边对"什么算同一个答案"的判断如果不是一套标准，报告就会互相打架。
    """
    text = (text or "").replace("&#39;", "'").replace("&amp;", "&")
    text = re.sub(r"[\s\u00a0]+", "", text)
    text = re.sub(r"[，,。.、:：;；\"'“”‘’()（）]", "", text)
    return text.lower()


def _decision_tuple(payload: dict) -> tuple:
    """把一个决策压成可比较的元组 (动作, 目标, 附加参数, 归一化答案)。

    刻意**不走 check_action**：老决策里本来就有非法的（它正是我们要回放的样本），
    校验只会把它拦在门外，让"被拒的步"失去对比资格。
    附加参数按动作类型挑对应字段：type 看 text、select 看 option、goto 看 url。
    """
    action = str(payload.get("action") or "").strip().lower()
    target = str(payload.get("target") or "")
    extra = ""
    if action == "type":
        extra = str(payload.get("text") or "")
    elif action == "select":
        extra = str(payload.get("option") or "")
    elif action == "goto":
        extra = str(payload.get("url") or "")
    elif action == "scroll":
        extra = str(payload.get("direction") or "")
    answer = _norm(str(payload.get("answer") or ""))
    return (action, target, extra, answer)


def _is_grounded(answer: str, snapshot_text: str) -> bool:
    """幻觉可信度检查：答案（归一化后）是否真的出现在快照文本里。

    只在"新决策提前 finish"时用：提前收工本来是好事，
    但如果答案是无中生有编的，这个"好事"比绕路更危险——
    它会带着一个自信的错误答案结束任务。
    """
    a = _norm(answer)
    if not a:
        return False
    return a in _norm(snapshot_text)


def compare_decisions(
    *,
    old_payload: dict | None,
    new_payload: dict | None,
    new_error: str,
    old_ok: bool,
    snapshot_text: str,
) -> tuple[str, str]:
    """对比一次老决策和一次新决策，返回 (verdict, 人话解释)。

    分支顺序就是"排除法"的顺序：先排除没有可比对象的情况，
    再排除"新的直接不合法"的情况，然后才轮得到语义对比。
    """
    # 老决策本体缺失：通常是模型当时的输出完全解析失败（payload=None）
    if not isinstance(old_payload, dict):
        return "missing_old_decision", "老决策没有可解析的输出，无法对比"

    # 新决策在 check_action 就被判非法：无论老决策是什么都是退化——
    # 连"老决策本来就被拒"的场景也一样算（它说明 prompt 没把模型救回来）
    if new_error:
        return "new_invalid", f"新决策被判非法：{new_error}"

    # new_error 为空但 payload 也不是 dict：防御性分支，理论上到不了
    if not isinstance(new_payload, dict):
        return "new_invalid", "新决策没有可解析的输出"

    # 老决策被拒 + 新决策合法 → 修复。判定放在四象限之前：
    # 从"被判非法"到"能走通"本身就是改进，动作是否相同反而次要
    if not old_ok:
        return "fixed_rejection", "老决策这一步被判非法，新决策给出了合法动作"

    old_action = str(old_payload.get("action") or "").strip().lower()
    new_action = str(new_payload.get("action") or "").strip().lower()
    old_finish = old_action == "finish"
    new_finish = new_action == "finish"

    # ---- finish / 非 finish 的四象限 ----
    if old_finish and new_finish:
        old_answer = str(old_payload.get("answer") or "")
        new_answer = str(new_payload.get("answer") or "")
        if _norm(old_answer) == _norm(new_answer):
            return "same", "两次都 finish，答案一致"
        ratio = difflib.SequenceMatcher(
            None, _norm(old_answer), _norm(new_answer)
        ).ratio()
        if ratio >= ANSWER_SIMILARITY_THRESHOLD:
            return "same", f"两次都 finish，答案高度相似（{ratio:.2f}）"
        return "different_answer", (
            f"答案不同（相似度 {ratio:.2f}）：老={old_answer[:60]!r} 新={new_answer[:60]!r}"
        )

    if old_finish and not new_finish:
        new_desc = _decision_tuple(new_payload)
        return "worse_later_finish", (
            f"老决策直接 finish，新决策却去 {new_desc[0]} {new_desc[1]}——多走了弯路"
        )

    if not old_finish and new_finish:
        new_answer = str(new_payload.get("answer") or "")
        verdict = "better_earlier_finish"
        note = f"新决策提前 finish，答案={new_answer[:60]!r}"
        if not _is_grounded(new_answer, snapshot_text):
            # 好事变可疑：提前收工 + 答案在快照里找不到出处
            note += "；⚠️ 答案未在快照正文中出现（疑似幻觉），需人工核对"
        return verdict, note

    # 两个都是动作：比"做的是不是同一件事"
    old_t = _decision_tuple(old_payload)
    new_t = _decision_tuple(new_payload)
    if old_t == new_t:
        return "same", "动作、目标、参数完全一致"
    if old_t[0] == new_t[0] and old_t[1] != new_t[1]:
        return "different_target", f"同为 {old_t[0]}，老点 {old_t[1]}、新点 {new_t[1]}"
    return "different_action", f"动作不同：老 {old_t[0]} {old_t[1]}，新 {new_t[0]} {new_t[1]}"


async def replay_task(task_id: str, *, llm: LLMClient | None = None, limit: int | None = None) -> dict:
    """回放一个历史任务的所有可回放步骤。

    返回 {"task_id", "goal", "results", "skipped"}：
    - results 里每条含 step / old_action / new_action / verdict / why
    - skipped 是"没有冻结输入"的步数（M5a 之前的老任务整条都是这个），
      **跳过必须显式计数**，不能悄悄当成"一致"——那会让报告虚高。
    """
    meta = load_meta(task_id)
    if meta is None:
        raise FileNotFoundError(f"任务 {task_id} 没有 meta.json，无法回放")
    trace = load_trace(task_id)
    if not trace:
        raise ValueError(f"任务 {task_id} 的 trace.json 为空，无法回放")

    goal = str(meta.get("goal") or "")
    llm = llm or LLMClient()

    results: list[dict] = []
    skipped = 0

    for idx, entry in enumerate(trace):
        decision = entry.get("decision")
        # 老格式 / 不完整：静默跳过，但计数要留痕
        if not isinstance(decision, dict) or not decision.get("snapshot_text"):
            skipped += 1
            continue
        # limit 只限制"真正回放的步数"，超出后停止（不算 skipped）
        if limit is not None and len(results) >= limit:
            break

        frozen_snapshot: PageSnapshot | None = decision.get("snapshot")
        step_no = int(entry.get("step") or 0)

        # ---- 重建"当时这一问" ----
        # history 用 trace[:idx]：trace 是按记录顺序 append 的，
        # 切片正好是"这一步被记录之前"的全部条目（含同一步里更早被拒的尝试），
        # 与 workflow 当时传入 build_user_prompt 的内容一致。
        # 新版条目多出来的 decision 键不会被 prompt 渲染（只读 step/action/ok/error），
        # 所以直接传新版行是安全的。
        try:
            user_prompt = build_user_prompt(
                goal=goal,
                step=step_no,
                max_steps=int(decision.get("max_steps") or 0),
                snapshot_text=str(decision.get("snapshot_text") or ""),
                history=trace[:idx],
                error_hint=str(decision.get("error_hint") or ""),
                notice=str(decision.get("notice") or ""),
                plan=list(decision.get("plan") or []),
                progress=list(decision.get("progress") or []),
                # P5a-4：用户否决段落。老条目没有这个键 → 空串 → prompt 一字不变，
                # P5a-4 之前的所有回放结果仍然可比。新条目带上它，重放出来的
                # 才是"当时真正问出去的那个问题"。
                rejection=str(decision.get("rejection") or ""),
            )

            # ---- 用现在的代码重新问一次 ----
            new_payload = await llm.decide(SYSTEM_PROMPT, user_prompt)
            elements = {
                el["id"]: el
                for el in (frozen_snapshot or {}).get("elements", [])
                if isinstance(el, dict) and el.get("id")
            }
            new_error = ""
            try:
                check_action(
                    parse_raw(new_payload),
                    elements=elements,
                    allowed_hosts=settings.allowed_hosts,
                )
            except ActionError as exc:
                new_error = str(exc)
        except LLMFormatError as exc:
            # 模型这次输出垃圾：本身就是一条"新的退化了"的证据，不能让整批回放崩掉
            new_payload = None
            new_error = f"模型输出不可解析：{exc}"
        except Exception as exc:  # 回放自身的其他故障（网络断、代码 bug）
            results.append(
                {
                    "step": step_no,
                    "old_action": str(entry.get("action") or ""),
                    "new_action": "",
                    "verdict": "replay_error",
                    "why": f"回放失败：{type(exc).__name__}: {exc}",
                }
            )
            continue

        verdict, why = compare_decisions(
            old_payload=decision.get("payload"),
            new_payload=new_payload,
            new_error=new_error,
            old_ok=bool(entry.get("ok")),
            snapshot_text=str(decision.get("snapshot_text") or ""),
        )
        results.append(
            {
                "step": step_no,
                "old_action": str(entry.get("action") or ""),
                "new_action": _describe_payload(new_payload, new_error),
                "verdict": verdict,
                "why": why,
            }
        )

    return {"task_id": task_id, "goal": goal, "results": results, "skipped": skipped}


def _describe_payload(payload: dict | None, error: str) -> str:
    """新决策的一行人话描述（写进报告表格用）。"""
    if error and not isinstance(payload, dict):
        return f"[不可解析] {error[:60]}"
    if not isinstance(payload, dict):
        return "[无输出]"
    action = str(payload.get("action") or "?")
    target = str(payload.get("target") or "")
    if action == "finish":
        return f"finish {str(payload.get('answer') or '')[:40]!r}"
    return f"{action} {target}".strip()


def summarize(replays: list[dict]) -> dict:
    """汇总多任务的回放结果。

    ⚠️ 分母是"真正回放了 steps 数"（sum(len(results))），
    不是"历史总步数"——skipped 的老步没有参与对比，不能摊进百分比。
    """
    total = sum(len(r.get("results") or []) for r in replays)
    counter: Counter[str] = Counter()
    for r in replays:
        for item in r.get("results") or []:
            counter[str(item.get("verdict"))] += 1

    same = counter.get("same", 0)
    return {
        "replayed": total,
        "skipped": sum(int(r.get("skipped") or 0) for r in replays),
        "same": same,
        "same_rate": round(same / total * 100, 1) if total else 0.0,
        "good": sum(counter.get(v, 0) for v in GOOD_VERDICTS),
        "bad": sum(counter.get(v, 0) for v in BAD_VERDICTS),
        "review": sum(counter.get(v, 0) for v in REVIEW_VERDICTS),
        "by_verdict": dict(counter),
    }


def render_report(replays: list[dict], summary: dict) -> str:
    """渲染成 markdown 报告：一张汇总表 + 每任务一张对比明细表。"""
    lines = [
        "# 回放对比报告（M5a）",
        "",
        "对比口径：老决策 = trace 冻结的模型输出；新决策 = 冻结输入重建 prompt 后重新问模型。",
        "",
        "## 汇总",
        "",
        f"- 回放步数：{summary['replayed']}（另有 {summary['skipped']} 步无冻结输入，跳过）",
        f"- 决策一致（same）：{summary['same']} / {summary['replayed']} = {summary['same_rate']}%",
        f"- 好事（prompt 改进生效）：{summary['good']}",
        f"- 坏事（prompt 改动致退化）：{summary['bad']}",
        f"- 待人工核对：{summary['review']}",
        "",
        "| verdict | 次数 |",
        "|---|---|",
    ]
    for verdict, count in sorted(summary["by_verdict"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {verdict} | {count} |")

    for r in replays:
        lines += [
            "",
            f"## 任务 {r['task_id']}",
            "",
            f"- 目标：{r['goal']}",
            f"- 回放 {len(r['results'])} 步，跳过 {r['skipped']} 步",
            "",
            "| 步 | 老决策 | 新决策 | verdict | 说明 |",
            "|---|---|---|---|---|",
        ]
        for item in r["results"]:
            old_a = str(item.get("old_action") or "").replace("|", "/")[:36]
            new_a = str(item.get("new_action") or "").replace("|", "/")[:36]
            why = str(item.get("why") or "").replace("|", "/")[:80]
            lines.append(
                f"| {item.get('step')} | {old_a} | {new_a} | {item.get('verdict')} | {why} |"
            )

    return "\n".join(lines) + "\n"
