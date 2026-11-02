"""job 层兜底的测试（P5b 补的防线）。

它在测试体系里的角色：守住**最坏的一种失败模式**——
任务"看起来还在跑"、实际上已经死了，而且没留下任何线索。

为什么单独一个文件而不是塞进 test_queue.py：
这个文件测的不是"队列能不能用"，而是"队列运行器崩溃时任务会不会留下终态"。
两者的失败后果完全不同：前者是功能缺失，后者是**用户无限等待 + 开发者零信息量**。
分开写，出问题时一眼知道是哪一类。
"""
from __future__ import annotations

from app import worker
from app.runner import task_runner


async def test_mark_crashed_writes_terminal_state(monkeypatch):
    """崩溃兜底必须把状态钉成 error，并且 note 里带上异常类型。

    为什么 note 里要带类型：事后只有一个地方能告诉你"炸的是什么"。
    只写"执行进程内部错误"等于什么都没说——用户和开发者都得再来问一遍。
    """
    saved: dict = {}
    status_calls: list[tuple] = []

    monkeypatch.setattr(
        task_runner.files, "load_meta", lambda tid: {"task_id": tid, "status": "running"}
    )
    monkeypatch.setattr(task_runner.files, "save_meta", lambda tid, meta: saved.update(meta))

    async def fake_set_status(task_id, status, note=""):
        status_calls.append((task_id, status, note))

    monkeypatch.setattr(task_runner.repo, "set_status", fake_set_status)

    await task_runner.mark_crashed("t-1", RuntimeError("engine exploded"))

    assert saved["status"] == "error"
    assert "RuntimeError" in saved["note"], saved["note"]
    assert "engine exploded" in saved["note"], saved["note"]
    assert saved["finished_at"], "没写结束时间的话，统计里这条会一直是'未完成'"
    # DB 也要写：meta.json 只是"DB 挂了也能读"的兜底，不能只写它
    assert status_calls == [("t-1", "error", saved["note"])]


async def test_mark_crashed_survives_its_own_failure(monkeypatch):
    """兜底自己也失败时，**不许再抛**。

    场景：数据库和磁盘同时不可用。这时候如果 mark_crashed 抛出去，
    调用方（worker 的 except 块）会看到一个新异常，而**原始异常**的堆栈就丢了。
    排查难度从"一个 bug"变成"两个 bug，而且你还不知道第一个是什么"。
    """
    monkeypatch.setattr(task_runner.files, "load_meta", lambda tid: {})
    monkeypatch.setattr(task_runner.files, "save_meta", lambda tid, meta: None)

    async def broken_set_status(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(task_runner.repo, "set_status", broken_set_status)

    # 不抛 —— 这条测试的价值就在于"调用它不会抛"
    await task_runner.mark_crashed("t-2", ValueError("original problem"))


def test_exc_text_is_truncated():
    """异常说明要截断。

    为什么不放全文：note 是给用户看的，不是日志。SQL 报错能带上整条语句、
    某些库的异常会带上请求参数——那些东西不该出现在前端的一行小字里。
    完整堆栈在 worker 的 log.exception 里，只有我们看得到。
    """
    short = task_runner._exc_text(ValueError("boom"))
    assert short == "ValueError: boom"

    long = task_runner._exc_text(RuntimeError("x" * 500))
    assert len(long) <= 301, f"没截断：{len(long)}"
    assert long.endswith("…")


async def test_run_task_swallows_exception_and_marks_crashed(monkeypatch):
    """run_task 遇到 execute_task 抛异常时：**正常返回**，并且写了兜底终态。

    这条是本文件的核心。两个断言分别对应两个决定：

    ① `result == task_id`（不重新抛出）——
       抛出去 arq 可能重试这个 job，而任务执行是有副作用的：
       重试 = 「提交」被点第二次、表单被填第二遍。
       一个"状态不准"的问题，不值得用"动作执行两遍"去换。

    ② `crashed` 里有记录 ——
       光吞掉异常不写终态，就变成了"静默失败"，那比抛出更糟：
       任务永久 running，谁也不知道它早就死了。
       **吞异常和写终态必须成对出现**。
    """
    async def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    crashed: list[tuple] = []

    async def fake_mark(task_id, exc):
        crashed.append((task_id, str(exc)))

    monkeypatch.setattr(task_runner, "execute_task", boom)
    monkeypatch.setattr(task_runner, "mark_crashed", fake_mark)

    # ctx 里的 browser 用任意对象代替：这条路径不应该真的碰浏览器
    result = await worker.run_task({"browser": object()}, "t-3", "随便做点什么", "http://x")

    assert result == "t-3", "重新抛出了异常 → arq 会重试 → 动作可能执行两遍"
    assert crashed == [("t-3", "engine exploded")], "吞了异常却没写终态 → 任务永久 running"


async def test_resume_task_swallows_exception_and_marks_crashed(monkeypatch):
    """续跑路径同样要兜。

    续跑崩溃比首跑更尴尬：`resume()` 是"先改状态再入队"，
    所以崩溃时任务停在 queued、审批项已经是 approved ——
    界面显示"排队中"、库里显示"已批准"，两份真相互相矛盾，而且都不会自己前进。
    """
    async def boom(*args, **kwargs):
        raise KeyError("missing context")

    crashed: list[tuple] = []

    async def fake_mark(task_id, exc):
        crashed.append((task_id, str(exc)))

    monkeypatch.setattr(task_runner, "execute_resume", boom)
    monkeypatch.setattr(task_runner, "mark_crashed", fake_mark)

    result = await worker.resume_task({"browser": object()}, "t-4", 99)

    assert result == "t-4"
    # ⚠️ 这里用 in 而不是相等：`str(KeyError("x"))` 返回的是 `"'x'"`（带引号）——
    # KeyError 的 __str__ 取的是参数的 repr，这是 Python 的一个老怪癖。
    # 写成相等会让测试莫名其妙地红一次，然后有人去改代码"修"一个不存在的 bug。
    assert crashed and crashed[0][0] == "t-4"
    assert "missing context" in crashed[0][1]
