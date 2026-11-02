"""队列相关的纯逻辑测试：不连 redis、不启浏览器。

只测两件"写错了会静默降级"的事：
1. 关掉队列开关时，`_enqueue` 必须直接返回 False，**不能去试连 redis**
2. redis 熔断期内，同样不能去试连（否则每次提交都要白等一次连接超时）

为什么值得单独测：降级路径平时不执行，一旦写错（比如忘了 return、或者熔断判断反了），
症状是"有 redis 却不用队列"或者"没 redis 时每次提交卡 2 秒"，
两种都不会报错，只会让人以为"队列没生效"。
"""
from __future__ import annotations

import time

from app.runner import task_runner


class _FakeSettings:
    """最小的 settings 替身：只放 _enqueue 会读到的字段。"""

    queue_enabled = False
    redis_url = "redis://127.0.0.1:1/0"   # 故意指向一个不存在的端口
    max_jobs = 3


async def test_enqueue_returns_false_when_disabled(monkeypatch):
    """QUEUE_ENABLED=false 时必须立刻放弃，不能去连 redis。"""
    monkeypatch.setattr(task_runner, "settings", _FakeSettings())
    # 把熔断清掉，否则测试可能因为上一条用例的副作用直接返回 False，测不到真正的分支
    monkeypatch.setattr(task_runner, "_redis_down_until", 0.0)

    started = time.monotonic()
    ok = await task_runner._enqueue("t-disabled", "goal", "http://x/")
    elapsed = time.monotonic() - started

    assert ok is False
    # 指向的是不存在的端口，如果真的去连会耗时甚至抛错；这里必须"瞬间返回"
    assert elapsed < 0.2, f"关掉队列还在尝试连接（耗时 {elapsed:.2f}s）"


async def test_enqueue_skips_during_fuse(monkeypatch):
    """熔断期内不能重试：redis 挂着时每次提交都白等一次超时是最糟的体验。"""
    fake = _FakeSettings()
    fake.queue_enabled = True
    monkeypatch.setattr(task_runner, "settings", fake)
    # 熔断到 60 秒之后
    monkeypatch.setattr(task_runner, "_redis_down_until", time.monotonic() + 60)

    started = time.monotonic()
    ok = await task_runner._enqueue("t-fuse", "goal", "http://x/")
    elapsed = time.monotonic() - started

    assert ok is False
    assert elapsed < 0.2, f"熔断期内还在尝试连接（耗时 {elapsed:.2f}s）"
