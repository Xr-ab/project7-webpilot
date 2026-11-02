"""回放器测试（M5a）。

造数策略：测试里的假 trace 行必须和 workflow._record 落盘的形状一致——
尤其 decision blob 的 7 个键一个都不能少，否则测的不是真实数据形状。
LLM 用 _ScriptedLLM 假对象：按剧本吐 payload、记录收到的 prompt，
这样"prompt 重建是否保真"可以直接断言。
"""
from __future__ import annotations

import json

import pytest

from app.agent.llm import LLMFormatError
from app.eval import replayer
from app.eval.replayer import (
    compare_decisions,
    render_report,
    replay_task,
    summarize,
)
from app.storage import files as files_mod

# ---------------------------------------------------------------- 造数辅助


def _snapshot() -> dict:
    """一份最小 PageSnapshot：两个元素（按钮 e1、输入框 e2）+ 含答案的正文。"""
    return {
        "url": "http://test.local/page",
        "title": "测试页",
        "headings": ["页面标题"],
        "text": "这是正文内容。页面上写着 Albert Einstein 的一句话。",
        "text_truncated": False,
        "elements": [
            {"id": "e1", "role": "button", "text": "按钮一"},
            {"id": "e2", "role": "input", "text": "搜索框"},
        ],
    }


def _entry(
    step: int,
    action: str,
    payload: dict | None,
    *,
    ok: bool = True,
    plan: list[str] | None = None,
    progress: list[int] | None = None,
    snapshot: dict | None = None,
    snapshot_text: str | None = None,
    error_hint: str = "",
    notice: str = "",
) -> dict:
    """造一条与 workflow._record 形状一致的 trace 行（含 decision）。"""
    snap = snapshot if snapshot is not None else _snapshot()
    return {
        "step": step,
        "url": snap["url"],
        "thought": (payload or {}).get("thought", ""),
        "action": action,
        "ok": ok,
        "error": None,
        "duration_ms": 100,
        "elements_total": len(snap.get("elements") or []),
        "screenshot": None,
        "llm_usage": {},
        "decision": {
            "snapshot_text": snapshot_text if snapshot_text is not None else snap["text"],
            "snapshot": snap,
            "payload": payload,
            "plan": plan or [],
            "progress": progress or [],
            "notice": notice,
            "error_hint": error_hint,
            "max_steps": 12,
        },
    }


class _ScriptedLLM:
    """按剧本吐 payload 的假 LLM；记录收到的 prompt 供保真断言。"""

    def __init__(self, script: list):
        self._script = list(script)
        self.prompts: list[str] = []
        self.last_usage: dict = {}

    async def decide(self, system: str, user: str) -> dict:
        self.prompts.append(user)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def runs_dir(tmp_path, monkeypatch):
    """把 RUNS_ROOT 指到临时目录，测试不碰真实 runs/。

    monkeypatch 的是 files 模块的属性：replayer 里的 load_meta/load_trace
    内部经 task_dir() 动态读 RUNS_ROOT，所以改模块属性就能生效。
    """
    monkeypatch.setattr(files_mod, "RUNS_ROOT", tmp_path)
    return tmp_path


def _write_task(runs_dir, task_id: str, meta: dict, trace: list[dict]) -> None:
    d = runs_dir / task_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "trace.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")


_META = {"task_id": "t1", "goal": "测试目标", "status": "success", "steps": 1}


# ---------------------------------------------------------------- compare_decisions

_P_CLICK_E1 = {"thought": "点它", "action": "click", "target": "e1"}
_P_SCROLL = {"thought": "滚一下", "action": "scroll", "direction": "down"}
_P_FINISH_EINSTEIN = {"thought": "读到了", "action": "finish", "answer": "Albert Einstein"}


def test_compare_same_identical():
    verdict, why = compare_decisions(
        old_payload=dict(_P_CLICK_E1), new_payload=dict(_P_CLICK_E1),
        new_error="", old_ok=True, snapshot_text="正文",
    )
    assert verdict == "same"


def test_compare_same_by_similarity():
    """答案只差标点/空白时算一致（与 eval_real 的 norm 口径对齐）。"""
    old = {"action": "finish", "answer": "共 1 条结果"}
    new = {"action": "finish", "answer": "共1条结果！"}
    verdict, _ = compare_decisions(
        old_payload=old, new_payload=new, new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "same"


def test_compare_same_by_high_ratio():
    """norm 后不完全相等但相似度 >= 0.8 也算 same。"""
    old = {"action": "finish", "answer": "共 1 条结果"}
    new = {"action": "finish", "answer": "1 条结果"}
    verdict, why = compare_decisions(
        old_payload=old, new_payload=new, new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "same"
    assert "相似" in why


def test_compare_different_answer():
    old = {"action": "finish", "answer": "Albert Einstein"}
    new = {"action": "finish", "answer": "Marilyn Monroe"}
    verdict, why = compare_decisions(
        old_payload=old, new_payload=new, new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "different_answer"
    assert "相似度" in why


def test_compare_better_earlier_finish_grounded():
    """老决策还在 scroll，新决策提前 finish 且答案在快照里 → 好事。"""
    verdict, why = compare_decisions(
        old_payload=dict(_P_SCROLL), new_payload=dict(_P_FINISH_EINSTEIN),
        new_error="", old_ok=True, snapshot_text="页面上写着 Albert Einstein 的一句话",
    )
    assert verdict == "better_earlier_finish"
    assert "疑似幻觉" not in why


def test_compare_better_earlier_finish_hallucination():
    """提前 finish 但答案在快照里找不到出处 → 标记疑似幻觉，提醒人工核对。"""
    new = {"action": "finish", "answer": "亚里士多德"}
    verdict, why = compare_decisions(
        old_payload=dict(_P_SCROLL), new_payload=new,
        new_error="", old_ok=True, snapshot_text="页面上写着 Albert Einstein",
    )
    assert verdict == "better_earlier_finish"
    assert "疑似幻觉" in why


def test_compare_worse_later_finish():
    """老决策一步 finish，新决策反而去做动作 → 退化。"""
    verdict, why = compare_decisions(
        old_payload=dict(_P_FINISH_EINSTEIN), new_payload=dict(_P_SCROLL),
        new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "worse_later_finish"


def test_compare_fixed_rejection():
    """老决策本来被拒（old_ok=False），新决策合法 → 好事。"""
    verdict, _ = compare_decisions(
        old_payload=dict(_P_CLICK_E1), new_payload=dict(_P_CLICK_E1),
        new_error="", old_ok=False, snapshot_text="",
    )
    assert verdict == "fixed_rejection"


def test_compare_new_invalid():
    """老决策合法，新决策被判非法 → 退化。"""
    verdict, why = compare_decisions(
        old_payload=dict(_P_CLICK_E1), new_payload=dict(_P_CLICK_E1),
        new_error="编号 e9 不在本轮快照里", old_ok=True, snapshot_text="",
    )
    assert verdict == "new_invalid"
    assert "e9" in why


def test_compare_missing_old_decision():
    verdict, _ = compare_decisions(
        old_payload=None, new_payload=dict(_P_CLICK_E1),
        new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "missing_old_decision"


def test_compare_different_target():
    old = {"action": "click", "target": "e7"}
    new = {"action": "click", "target": "e9"}
    verdict, why = compare_decisions(
        old_payload=old, new_payload=new, new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "different_target"
    assert "e7" in why and "e9" in why


def test_compare_different_action():
    verdict, why = compare_decisions(
        old_payload=dict(_P_CLICK_E1), new_payload=dict(_P_SCROLL),
        new_error="", old_ok=True, snapshot_text="",
    )
    assert verdict == "different_action"


# ---------------------------------------------------------------- replay_task


@pytest.mark.asyncio
async def test_replay_task_rebuilds_prompt_from_frozen_inputs(runs_dir):
    """prompt 重建保真：goal / 步数 / plan 进度 / 历史 / 快照都要在场。"""
    snap = _snapshot()
    # 第 1 步成功 click e1；第 2 步是我们要回放的决策
    trace = [
        _entry(1, "click e1", dict(_P_CLICK_E1), snapshot=snap),
        _entry(
            2, "scroll down", dict(_P_SCROLL),
            plan=["输入关键词", "点搜索"], progress=[1], snapshot=snap,
        ),
    ]
    _write_task(runs_dir, "t1", _META, trace)

    llm = _ScriptedLLM([dict(_P_CLICK_E1)])
    out = await replay_task("t1", llm=llm)

    assert out["skipped"] == 0
    assert len(out["results"]) == 2
    prompt = llm.prompts[1]  # 第 2 步收到的 prompt
    assert "测试目标" in prompt
    assert "第 2/12 步" in prompt
    assert "1. 输入关键词（已完成）" in prompt
    assert "[已执行]" in prompt and "第1步 click e1 → 成功" in prompt
    assert snap["text"] in prompt  # 冻结的 snapshot_text 原样在场


@pytest.mark.asyncio
async def test_replay_task_skips_legacy_entries(runs_dir):
    """M5a 之前的老 trace 没有 decision：跳过并计数，绝不冒充"一致"。"""
    legacy = {
        "step": 1, "url": "http://x", "thought": "", "action": "click e1",
        "ok": True, "error": None, "duration_ms": 1, "elements_total": 1,
        "screenshot": None, "llm_usage": {},
    }
    _write_task(runs_dir, "t1", _META, [legacy])
    llm = _ScriptedLLM([])
    out = await replay_task("t1", llm=llm)
    assert out["results"] == []
    assert out["skipped"] == 1
    assert llm.prompts == []  # 老条目根本不该去问模型


@pytest.mark.asyncio
async def test_replay_task_survives_llm_garbage(runs_dir):
    """这次模型输出垃圾（LLMFormatError）：记为 new_invalid，不炸整批。"""
    trace = [_entry(1, "scroll down", dict(_P_SCROLL))]
    _write_task(runs_dir, "t1", _META, trace)
    llm = _ScriptedLLM([LLMFormatError("输出里找不到 JSON 对象")])
    out = await replay_task("t1", llm=llm)
    assert out["results"][0]["verdict"] == "new_invalid"
    assert "不可解析" in out["results"][0]["new_action"]


@pytest.mark.asyncio
async def test_replay_task_replay_error_isolated(runs_dir):
    """回放自身出错（如 check 之外的异常）→ replay_error，后续步继续。"""
    snap = _snapshot()
    trace = [
        _entry(1, "click e1", dict(_P_CLICK_E1), snapshot=snap),
        _entry(2, "scroll down", dict(_P_SCROLL), snapshot=snap),
    ]
    _write_task(runs_dir, "t1", _META, trace)

    class _FlakyLLM(_ScriptedLLM):
        # 用独立计数器判断"第一次调用"：prompts 是在父类 decide 里 append 的，
        # 判断 prompts 长度会把时序搞反
        def __init__(self, script: list):
            super().__init__(script)
            self.calls = 0

        async def decide(self, system: str, user: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("网络断了")
            return await super().decide(system, user)

    llm = _FlakyLLM([dict(_P_SCROLL)])
    out = await replay_task("t1", llm=llm)
    assert out["results"][0]["verdict"] == "replay_error"
    assert out["results"][1]["verdict"] == "same"


@pytest.mark.asyncio
async def test_replay_task_missing_meta_raises(runs_dir):
    with pytest.raises(FileNotFoundError):
        await replay_task("不存在的任务", llm=_ScriptedLLM([]))


@pytest.mark.asyncio
async def test_replay_task_limit(runs_dir):
    snap = _snapshot()
    trace = [
        _entry(1, "click e1", dict(_P_CLICK_E1), snapshot=snap),
        _entry(2, "click e1", dict(_P_CLICK_E1), snapshot=snap),
        _entry(3, "click e1", dict(_P_CLICK_E1), snapshot=snap),
    ]
    _write_task(runs_dir, "t1", _META, trace)
    llm = _ScriptedLLM([dict(_P_CLICK_E1)])
    out = await replay_task("t1", llm=llm, limit=1)
    assert len(out["results"]) == 1
    assert out["skipped"] == 0  # limit 截断不算 skipped


# ---------------------------------------------------------------- summarize / render


def test_summarize_denominator_is_replayed():
    """分母必须是"真正回放的步数"，skipped 的老步不能摊进百分比。"""
    replays = [
        {"task_id": "t1", "goal": "", "skipped": 3, "results": [
            {"step": 1, "old_action": "a", "new_action": "a", "verdict": "same", "why": ""},
            {"step": 2, "old_action": "a", "new_action": "b", "verdict": "different_target", "why": ""},
        ]},
        {"task_id": "t2", "goal": "", "skipped": 1, "results": [
            {"step": 1, "old_action": "a", "new_action": "a", "verdict": "same", "why": ""},
            {"step": 2, "old_action": "a", "new_action": "b", "verdict": "worse_later_finish", "why": ""},
            {"step": 3, "old_action": "a", "new_action": "a", "verdict": "better_earlier_finish", "why": ""},
        ]},
    ]
    s = summarize(replays)
    assert s["replayed"] == 5
    assert s["skipped"] == 4
    assert s["same"] == 2
    assert s["same_rate"] == 40.0
    assert s["good"] == 1
    assert s["bad"] == 1
    assert s["review"] == 1


def test_render_report_contains_tables():
    replays = [
        {"task_id": "t1", "goal": "测试目标", "skipped": 2, "results": [
            {"step": 1, "old_action": "click e1", "new_action": "click e1",
             "verdict": "same", "why": "动作一致"},
        ]},
    ]
    report = render_report(replays, summarize(replays))
    assert "回放对比报告" in report
    assert "| t1 |" not in report  # 表格是逐行渲染，任务标题是二级标题
    assert "## 任务 t1" in report
    assert "click e1" in report
