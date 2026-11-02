"""登录失败限流（P5 收尾补的 M4 遗留）。

它在系统里的角色：**给 /login 加一道"试错有代价"的闸**。
没有它，任何人可以对同一个 email 无限次试密码（撞库），
bcrypt 的慢算只是让每次尝试贵一点，挡不住"一夜试一万次"。

为什么按 **email** 维度计数、不按 IP：
本机/内网部署的所有真实用户共享同一个出口 IP，按 IP 限流会把
整个办公室锁在一起；而撞库的典型形态就是"盯着一个账号试"，
email 维度正好打在这个点上。换 email 散打的攻击面由注册校验
（8 位下限）和 bcrypt 承担——多层防御各管一段，没有银弹。

为什么用 Redis 而不是进程内字典：
API 进程会重启（重启清零 = 锁定期形同虚设）；而且这是"生产上
必须跨进程共享"的典型状态，用现成的 Redis 没有额外依赖
（arq 已经在用同一台 Redis），面试也讲得清楚。

⚠️ **fail-open 的取舍**：Redis 连不上时选择**放行**而不是锁定。
理由：限流是增强项，Redis 挂了把全部用户锁在门外，比"暂时少一层
防爆破"糟糕得多（和审批落库失败不让任务崩是同一个哲学——
安全增强不能变成单点故障）。生产环境应该在 except 分支里加告警，
本项目的等价物是把这件事写进注释和测试。
"""
from __future__ import annotations

import logging

from arq.connections import RedisSettings, create_pool

from app.config import settings

log = logging.getLogger(__name__)

# 键前缀带上用途和项目名：这台 Redis（6380）可能被同机器的其他东西共用，
# 裸的 "fail:{email}" 太容易撞名
_PREFIX = "webpilot:login-fail:"

_pool = None


async def _client():
    """懒加载的 Redis 连接池。

    为什么不做成模块级 asyncio.create_task 预热：测试里要能 monkeypatch 掉
    整个连接，懒加载让"没连过 = 没副作用"，测试和降级路径都干净。
    """
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


def _key(email: str) -> str:
    return f"{_PREFIX}{email.strip().lower()}"


async def blocked(email: str) -> bool:
    """这个 email 当前是否处于锁定期。任何 Redis 故障都放行（fail-open）。"""
    try:
        client = await _client()
        count = await client.get(_key(email))
        return int(count or 0) >= settings.login_max_failures
    except Exception as exc:
        log.warning("登录限流查询失败，放行（fail-open）：%s", exc)
        return False


async def record_failure(email: str) -> None:
    """记一次登录失败。第一次失败时顺带设置过期，之后的 INCR 不重置计时。

    为什么过期挂在第一次而不是最后一次失败上：固定窗口简单且够用；
    滑动窗口（每次失败都续期）是更严的防法，但会让"手滑 4 次"的人
    背着一个越背越长的尾巴，对内网工具不值得。
    """
    try:
        client = await _client()
        key = _key(email)
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, settings.login_lockout_seconds)
    except Exception as exc:
        log.warning("登录失败计数写入失败，跳过（fail-open）：%s", exc)


async def clear(email: str) -> None:
    """登录成功后清零。失败记录只该跟着"还在失败"走，登进来了就该翻篇。"""
    try:
        client = await _client()
        await client.delete(_key(email))
    except Exception as exc:
        log.warning("登录失败计数清理失败，跳过（fail-open）：%s", exc)
