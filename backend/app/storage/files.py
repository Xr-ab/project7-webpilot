"""任务产物落盘：一个任务一个目录，全部产物都在里面。

为什么不用数据库（v0.1）：
- 现在只需要"按 task_id 取回一次运行的全部东西"，不需要查询、关联、并发写
- 一个目录 = 一份完整的运行证据，打包发给别人就能复现，比查库直观
- P3 要列表页、要多任务对比时再上 PostgreSQL，接口形状不用变（读 meta 的地方只认 dict）

目录结构：
    runs/<task_id>/
        meta.json     任务描述 + 最终状态 + 答案（一个任务一份，读它就知道结果）
        trace.json    每一步的 thought / action / 结果 / 耗时 / 截图路径
        step_1.png    每步执行完的截图（M5 才有，回放用）
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT
from app.storage import signing

# 所有任务产物都放这里（已在 .gitignore 里排除）
RUNS_ROOT = PROJECT_ROOT / "runs"

_ID_ALPHABET = "0123456789abcdefghijkmnpqrstuvwxyz"

# 原子写的重试次数。6 次、间隔 10/20/…/60ms，合计最坏等 210ms——
# 足够躲过"对方正在读"的那一瞬，又不至于让真正写不进去的情况（磁盘满/只读）
# 拖住任务几秒钟。
_REPLACE_RETRIES = 6


def new_task_id() -> str:
    """生成 task_id：时间前缀（可排序）+ 6 位随机（防同秒撞车）。

    没有用 uuid4：排查时想"从新到旧看任务"，时间前缀能直接字符串排序。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(_ID_ALPHABET, k=6))
    return f"{stamp}-{suffix}"


def task_dir(task_id: str) -> Path:
    """任务目录，不存在就建。task_id 只来自本模块生成，不接受外部拼路径。"""
    path = RUNS_ROOT / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, data: Any) -> None:
    """原子写：先写临时文件再替换。

    为什么要原子：M5 前端在轮询读 meta.json，如果正好读到"写了一半"的文件，
    json.load 会炸。rename 在同一分区上是原子的，读到的要么是旧的要么是新的。

    ⚠️ 为什么要对 replace 重试（P7-D1 T3 第 3 轮实测，一条任务被判 error）：
    **Windows 上"有人正在读"也会挡住 rename**——只要目标文件被别的进程以不带
    FILE_SHARE_DELETE 的方式打开着，MoveFileEx 就抛
    `PermissionError: [WinError 5] 拒绝访问`。
    本项目的形态正好踩满：API 进程每秒轮询读 meta.json，worker 同时每秒重写它，
    两边对上就是一发。POSIX 上 rename 不管这个（读到的还是旧 fd），所以这纯粹是
    平台差异，Linux 容器里跑一万次也复现不了——本地 Windows 才会中。
    重试是正确解法而不是"加锁"：写方只有一个（worker），冲突是瞬时的，
    十几毫秒就过去了；加锁会把这种瞬时竞争变成排队。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(_REPLACE_RETRIES):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            # 最后一次还失败就认账：清理临时文件后抛出去，别留一地 .tmp
            if attempt == _REPLACE_RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01 * (attempt + 1))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 文件损坏不该让整个任务是 500，返回默认值，上层按"没有"处理
        return default


def save_meta(task_id: str, meta: dict) -> None:
    """meta 是任务的"身份证 + 结论"，每次状态变化都整份覆盖写。"""
    _write_json(task_dir(task_id) / "meta.json", meta)


def load_meta(task_id: str) -> dict | None:
    data = _read_json(task_dir(task_id) / "meta.json", None)
    return data if isinstance(data, dict) else None


def save_trace(task_id: str, trace: list[dict]) -> None:
    _write_json(task_dir(task_id) / "trace.json", trace)


def load_trace(task_id: str) -> list[dict]:
    data = _read_json(task_dir(task_id) / "trace.json", [])
    return data if isinstance(data, list) else []


def screenshot_url(task_id: str, filename: str) -> str:
    """把磁盘上的截图路径翻译成前端能访问的 URL。

    M4 收尾：不再返回裸路径，而是带 HMAC 签名 + 过期时间（见 storage/signing.py）。
    根因是 `<img src>` 带不了 Authorization 头，凭证只能放进 URL。
    调用点都在**已认证**的接口里（get_trace），所以给谁签、签哪个文件是可控的。
    """
    return signing.sign_url(task_id, filename)


# 语音播报文件名固定，因为一个任务只有一个结论、只需要一个音频。
# 固定名字带来的额外好处：前端可以用固定 URL，不必等接口返回地址就能 <audio src>。
SPEECH_FILENAME = "speech.mp3"


def speech_path(task_id: str) -> Path:
    return task_dir(task_id) / SPEECH_FILENAME


def speech_url(task_id: str) -> str:
    """同样要签名：语音里念的就是任务答案，泄露程度不亚于 meta.json。"""
    return signing.sign_url(task_id, SPEECH_FILENAME)
