"""落盘层的单测：全部写到临时目录，不碰真实 runs/。

为什么值得测：这层出错的方式很隐蔽——文件写坏了、读回来是 None、
路径拼错，最后表现成"前端一直转圈"或者"任务列表空了"，很难往这里想。
"""
from __future__ import annotations

import re

from app.storage import files


def use_tmp_root(tmp_path, monkeypatch):
    """把 RUNS_ROOT 指到临时目录：不要在项目里留测试垃圾。"""
    monkeypatch.setattr(files, "RUNS_ROOT", tmp_path)
    return tmp_path


def test_new_task_id_shape():
    task_id = files.new_task_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-z]{6}", task_id), task_id


def test_meta_roundtrip(tmp_path, monkeypatch):
    use_tmp_root(tmp_path, monkeypatch)
    task_id = files.new_task_id()

    assert files.load_meta(task_id) is None  # 还没写过

    files.save_meta(task_id, {"task_id": task_id, "status": "queued"})
    loaded = files.load_meta(task_id)
    assert loaded is not None
    assert loaded["status"] == "queued"


def test_trace_roundtrip(tmp_path, monkeypatch):
    use_tmp_root(tmp_path, monkeypatch)
    task_id = files.new_task_id()

    assert files.load_trace(task_id) == []

    trace = [{"step": 1, "action": "click e1", "ok": True}]
    files.save_trace(task_id, trace)
    assert files.load_trace(task_id) == trace


def test_save_meta_overwrites_in_place(tmp_path, monkeypatch):
    """状态要能整份覆盖（queued → running → success），不是追加。"""
    use_tmp_root(tmp_path, monkeypatch)
    task_id = files.new_task_id()

    files.save_meta(task_id, {"status": "queued"})
    files.save_meta(task_id, {"status": "running"})
    files.save_meta(task_id, {"status": "success"})

    assert files.load_meta(task_id)["status"] == "success"
    # 覆盖写不该留下临时文件
    leftovers = list((tmp_path / task_id).glob("*.tmp"))
    assert leftovers == []


def test_corrupted_json_does_not_crash(tmp_path, monkeypatch):
    """文件被写坏（进程被杀、磁盘满）时返回默认值，不要让整个接口 500。"""
    use_tmp_root(tmp_path, monkeypatch)
    task_id = files.new_task_id()
    (files.task_dir(task_id) / "meta.json").write_text("{坏掉的 json", encoding="utf-8")

    assert files.load_meta(task_id) is None


def test_screenshot_url():
    """M4 收尾：截图地址从裸路径改成带签名的地址。

    原来断言的是 `== "/runs/abc-123/step_2.png"`。改成签名后这条断言必然失败——
    这是**预期内的失败**，因为接口形状确实变了（前端拿到的还是能直接塞进
    <img src> 的地址，只是多了 query）。这里改成断言"前缀 + 签名齐全"。
    """
    url = files.screenshot_url("abc-123", "step_2.png")
    assert url.startswith("/runs/abc-123/step_2.png?e=")
    assert "&sig=" in url


def test_screenshot_url_is_verifiable():
    """签名不是摆设：拿回来能被 verify 通过，且换了 task_id 就失效。"""
    from app.storage import signing

    url = files.screenshot_url("abc-123", "step_2.png")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert signing.verify("abc-123", "step_2.png", query["e"], query["sig"])
    assert not signing.verify("other-task", "step_2.png", query["e"], query["sig"])


def test_write_json_retries_when_target_is_busy(tmp_path, monkeypatch):
    """Windows 上目标文件正被读时 replace 会抛 WinError 5，必须重试（P7-D1 T3 实测）。

    这条测试是平台无关的：用 monkeypatch 让第一次 replace 抛 PermissionError，
    模拟"API 进程正在读 meta.json 的那一瞬"。真实复现只在 Windows 上出现，
    但重试逻辑本身哪都能验。
    """
    from pathlib import Path

    target = tmp_path / "meta.json"
    real_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "拒绝访问")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    files._write_json(target, {"status": "running"})

    assert calls["n"] == 2                      # 第一次撞上、第二次成功
    assert '"running"' in target.read_text(encoding="utf-8")
    # 成功后不能留 .tmp 垃圾
    assert not (tmp_path / "meta.json.tmp").exists()


def test_write_json_gives_up_and_cleans_tmp(tmp_path, monkeypatch):
    """一直写不进去要认账抛错，而不是留一地 .tmp 让人以为写成功了。"""
    from pathlib import Path

    target = tmp_path / "meta.json"

    def always_busy(self, dst):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(Path, "replace", always_busy)
    import pytest

    with pytest.raises(PermissionError):
        files._write_json(target, {"status": "running"})
    assert not (tmp_path / "meta.json.tmp").exists()
