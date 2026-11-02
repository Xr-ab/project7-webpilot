"""语音模块单测：只测纯逻辑，不碰网络、不加载模型。

为什么把边界划在这里：
- ASR 要加载 464MB 模型（10 秒），TTS 要连微软服务器（每次 1.5 秒）。
  把它们塞进单测，整个测试从 0.1 秒变成几十秒，断网还会随机红——测试一旦不稳定就没人跑了。
- 真正容易错、又最难靠人眼发现的是两件纯函数的事：
  ① 文本怎么洗（洗坏了念出来是一顿一顿的）
  ② 失败时念什么（曾经把 "click e1" 这种诊断信息念给用户听）
  这两种 bug 只有真听音频才能发现，所以必须有测试兜住。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.api.voice import _speech_text
from app.storage import files
from app.voice import tts


# ---------------- 文本清洗 ----------------
def test_clean_flattens_newlines():
    """列表式的答案换行很多，直接念会一顿一顿；压成逗号让停顿短一点。"""
    assert tts._clean("无线鼠标\n机械键盘\n显示器") == "无线鼠标，机械键盘，显示器"


def test_clean_strips_markdown_marks():
    """模型偶尔会在答案里带 ** 和 ` 这类记号，不洗掉会被念成"星号星号"。"""
    assert tts._clean("**共 6 件**商品") == "共 6 件商品"
    assert tts._clean("价格是 `59` 元") == "价格是 59 元"


def test_clean_collapses_repeated_punctuation():
    """换行前后本来就带逗号时，会拼出「，，」，TTS 会停两次，听着很怪。"""
    assert tts._clean("第一项，\n第二项") == "第一项，第二项"


def test_clean_trims_edges():
    assert tts._clean("   你好   ") == "你好"
    assert tts._clean("，你好，") == "你好"


def test_clean_empty_input():
    """空字符串要原样返回空，让上层能判断"没东西可念"。"""
    assert tts._clean("") == ""
    assert tts._clean("   \n  ") == ""


# ---------------- 念什么 ----------------
def test_speech_prefers_answer():
    """成功时有答案就念答案——念"已完成"对用户毫无信息量。"""
    meta = {"status": "success", "answer": "一共 6 条结果", "note": ""}
    assert _speech_text(meta) == "一共 6 条结果"


def test_speech_failure_uses_friendly_status():
    """失败时没有答案，念一句人话，而不是状态码。"""
    assert _speech_text({"status": "loop", "answer": "", "note": ""}).startswith("任务卡住了")
    assert _speech_text({"status": "timeout", "answer": "", "note": ""}).startswith("任务超时")
    assert _speech_text({"status": "nav_failed", "answer": "", "note": ""}).startswith("页面打不开")


def test_speech_never_reads_diagnostic_note():
    """回归测试：note 是给界面看的诊断信息，不能念出来。

    曾经的 bug：失败任务的 note 是「连续 3 次相同动作：click e1」，
    拼进去之后 TTS 念的是"连续三次相同动作，click e一"——
    英文动作名 + 元素编号，对耳朵是纯噪音。这条测试钉死这个行为。
    """
    meta = {"status": "loop", "answer": "", "note": "连续 3 次相同动作：click e1"}
    text = _speech_text(meta)
    assert "click" not in text
    assert "e1" not in text
    assert "连续 3 次" not in text


def test_speech_unknown_status_falls_back():
    """状态表里没有的新状态（以后加了但忘了补文案）也要能念出人话。"""
    assert _speech_text({"status": "some_new_state", "answer": ""}) == "任务没有给出结果。"


def test_speech_ignores_whitespace_only_answer():
    """answer 是纯空格时应该走状态分支，不能念一串空白。"""
    assert _speech_text({"status": "error", "answer": "   "}).startswith("任务出错了")


# ---------------- 产物路径 ----------------
def test_speech_path_and_url(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "RUNS_ROOT", tmp_path)
    task_id = files.new_task_id()

    assert files.speech_path(task_id) == tmp_path / task_id / "speech.mp3"
    # URL 用固定文件名，前端不必等接口返回就能拼出地址。
    # M4 收尾：地址带 HMAC 签名 + 过期时间（`<audio src>` 同样带不了 Authorization 头）
    url = files.speech_url(task_id)
    assert url.startswith(f"/runs/{task_id}/speech.mp3?e=")
    assert "&sig=" in url


# ---------------- 启动期 import 竞态 ----------------
def test_preload_import_covers_httpx2():
    """preload_import() 必须把后台预热线程要用的重模块都预载好。

    为什么这条测试要用子进程跑（麻烦但值得）：
    httpx2 会被 starlette.testclient 等模块顺手 import 进 sys.modules。
    如果在当前进程里断言 "httpx2 in sys.modules"，那条断言可能因为别人
    早就 import 过而**虚假通过**——而真正的 bug 恰恰是"后台线程第一次 import"。
    子进程是完全干净的解释器，能忠实复现启动瞬间的状态。

    依赖链（少预载一层就等于没修）：
        faster_whisper → faster_whisper.utils → huggingface_hub → httpx2
    """
    code = (
        "import sys;"
        "from app.voice import asr;"
        "asr.preload_import();"
        "missing = [m for m in ('faster_whisper', 'faster_whisper.utils', 'httpx2')"
        "           if m not in sys.modules];"
        "assert not missing, 'preload 漏了: %s' % missing;"
        "assert hasattr(sys.modules['httpx2'], 'URL'), 'httpx2 只初始化了一半'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],  # backend/，保证 app 包可导入
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
