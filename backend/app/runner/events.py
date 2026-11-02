"""任务事件桥：worker 每步 publish，语音会话 subscribe（P6b-B3）。

它解决的问题（P6 计划第三节问题 2）：
WebSocket 连在 **API 进程**上，任务跑在 **worker 进程**里——
两者除了 Redis 和数据库没有共享内存。要让"任务每走一步"实时出现在语音
会话的屏幕上，只能有一根跨进程的事件线。

为什么用 Redis pub/sub 而不是轮询：
- 延迟：毫秒级 vs 最多 500ms（P6 计划里的方案对比）；
- 成本：一个连接一个轮询循环，十个语音会话就是十个每秒两次的查询；
- "事件驱动"这个说法只有 pub/sub 在技术上站得住。

三个刻意的设计：

1. **只在有订阅者时发**（`PUBSUB NUMSUB` 先探一眼）。
   worker 是热路径，绝大多数任务没有语音会话在看——不探就发，
   等于给每个任务白加两次 Redis 往返。这是 P6 计划第十节的回归防护项。

2. **publish 失败一律吞**。
   事件推送是增强项：Redis 抖一下不该让任务失败。
   `log.debug` 而不是 warning——高频失败会把日志刷满。

3. **订阅侧可降级轮询**（`follow()` 内部处理）。
   Redis 不可用时退回"每 0.5s 查一次任务状态"，本地单机也能演示，
   和 P1-P5 的"降级不崩"是同一个风格。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from app.config import settings

log = logging.getLogger(__name__)

# 一个任务一个频道。加 webpilot: 前缀避免和同机其它项目撞车（和限流的 key 一致）
_CHANNEL = "webpilot:task:{task_id}"

# 终态事件名：`follow()` 见到它就结束迭代
FINAL_EVENT = "task.final"

# 轮询降级时的间隔。0.5s 是计划里定的值：比"人眼能察觉的延迟"再快一点，
# 又不至于把 API 进程的 CPU 吃满（每个连接每 2 秒一次 DB 查询）
POLL_INTERVAL = 0.5

# 进程内单例。redis.asyncio 客户端内部有连接池，每个会话建一个纯属浪费
_client = None


def channel_for(task_id: str) -> str:
    return _CHANNEL.format(task_id=task_id)


def _get_client():
    """懒加载 redis 客户端。返回 None 表示不可用（降级走轮询）。"""
    global _client
    if _client is None:
        try:
            import redis.asyncio as aioredis

            _client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                # 探活超时压短：pub/sub 连不上时要尽快降级，
                # 别让语音会话在"连 Redis"上白等几秒
                socket_connect_timeout=1.5,
                socket_timeout=5,
            )
        except Exception as exc:  # 连构造都失败（没装 redis 之类）：直接降级
            log.debug("Redis 客户端构造失败，事件桥将走轮询：%s", exc)
            return None
    return _client


async def publish_event(task_id: str, event: dict) -> None:
    """worker 侧：把一个事件发给订阅者。没有订阅者就什么都不做。

    event 必须自带 "event" 键（事件名），前端按它分发。
    """
    client = _get_client()
    if client is None:
        return
    try:
        channel = channel_for(task_id)
        # 先看有没有人在听。redis-py 的 pubsub_numsub 返回 [(channel, count)]
        subs = await client.pubsub_numsub(channel)
        if not subs or not subs[0][1]:
            return
        await client.publish(channel, json.dumps(event, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 —— 见模块说明第 2 条
        log.debug("事件发布失败（忽略）：%s", exc)


def step_event(row: dict) -> dict:
    """把一条 trace 行压成前端需要的最小事件。

    为什么不全量转发：trace 行里有 decision（整份快照，几十 KB），
    推到浏览器只是为了显示"第 3 步：点击 e5 成功"。按需裁剪是必要的。
    """
    return {
        "event": "task.step",
        "step": int(row.get("step") or 0),
        "action": str(row.get("action") or ""),
        "ok": bool(row.get("ok")),
    }


def final_event(meta: dict) -> dict:
    return {
        "event": FINAL_EVENT,
        "task_id": meta.get("task_id") or "",
        "status": meta.get("status") or "",
        "answer": meta.get("answer") or "",
        "note": meta.get("note") or "",
    }


async def follow(task_id: str) -> AsyncIterator[dict]:
    """API 侧：订阅一个任务的事件，直到 task.final。

    优先 Redis pub/sub；任何一步失败就整体退回轮询——
    不做"pub/sub 和轮询并行"，两路同时发事件会让前端收到重复的 task.final
    （关掉播放队列后又冒出一个"任务完成"，体验比慢一点差得多）。
    """
    client = _get_client()
    if client is not None:
        try:
            async for event in _follow_pubsub(client, task_id):
                yield event
            return
        except Exception as exc:  # 订阅过程中掉线：退回轮询继续供数
            log.debug("pub/sub 中断，切轮询继续跟任务 %s：%s", task_id, exc)
    async for event in _follow_poll(task_id):
        yield event


async def _follow_pubsub(client, task_id: str) -> AsyncIterator[dict]:
    channel = channel_for(task_id)
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(channel)
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                try:
                    event = json.loads(msg["data"])
                except (TypeError, json.JSONDecodeError):
                    continue  # 脏消息不该打断整条订阅
                yield event
                if event.get("event") == FINAL_EVENT:
                    return
                continue

            # 1 秒没消息时补一次状态检查：worker 崩了/被杀了不会发 final，
            # 没有这一步，前端会永远停在"进行中"（P5a 最怕的那种"没有痕迹的失败"）
            meta = await _peek_meta(task_id)
            if meta and meta.get("status") in _FINAL_STATUSES:
                yield final_event(meta)
                return
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 —— 收尾失败无所谓
            pass


async def _follow_poll(task_id: str) -> AsyncIterator[dict]:
    """降级：每 POLL_INTERVAL 查一次，用"新增的 trace 行"合成 step 事件。

    事件形状和 pub/sub 完全一致，前端不需要知道自己在哪条路上。
    """
    seen = 0
    while True:
        rows = await _peek_trace(task_id)
        for row in rows[seen:]:
            yield step_event(row)
        seen = len(rows)

        meta = await _peek_meta(task_id)
        if meta and meta.get("status") in _FINAL_STATUSES:
            yield final_event(meta)
            return
        await asyncio.sleep(POLL_INTERVAL)


# 哪些状态算"这一趟结束了"。waiting_approval 也算——它确实停下来了，
# 前端要收到一条 final 才能安心显示"去网页上批准"。
# 但不能复用 app.status.UNFINISHED 的反面：那个集合是给 REST 轮询用的语义，
# 两处混用会让"加一个状态"变成"改两个地方"（P5 的教训）。
_FINAL_STATUSES = {
    "success",
    "error",
    "timeout",
    "loop",
    "invalid_action",
    "nav_failed",
    "bad_request",
    "bad_url",
    "waiting_approval",
}


async def _peek_meta(task_id: str) -> dict | None:
    """读任务 meta（延迟 import 避免 task_runner ↔ events 循环引用）。"""
    try:
        from app.runner import task_runner

        return await task_runner.get_task(task_id)
    except Exception:  # noqa: BLE001
        return None


async def _peek_trace(task_id: str) -> list[dict]:
    try:
        from app.runner import task_runner

        return list(await task_runner.get_trace(task_id) or [])
    except Exception:  # noqa: BLE001
        return []
