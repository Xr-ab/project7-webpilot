"""任务产物 URL 的短期签名（M4 收尾）。

它解决的具体问题（不是假想风险，是本项目真实存在的越权链）：
    `/runs` 目录原来用 StaticFiles 裸挂 —— 任何人知道 task_id 就能读
    `/runs/<task_id>/meta.json`，拿到别人的 goal 和 answer。而 P4 的知识库
    问答会把别人的 task_id 返回给**所有**登录用户，两条线接起来就是：
        POST /api/v1/knowledge/ask  →  sources[].task_id  →  GET /runs/<id>/meta.json
    前门的 assert_owned 挡得再严，这条旁路不通就等于没锁门。

为什么凭证必须放在 URL 里，而不是 Authorization 头：
    截图是用 `<img src>` 加载的。浏览器发起图片请求时**不会**带自定义请求头
    —— 这是浏览器行为，改不了。所以凭证只能成为 URL 的一部分。
    工业界的同款做法就是 S3 / OSS 的预签名 URL（presigned URL）。

为什么用 HMAC 而不是复用 JWT：
    JWT 表达的是"我是谁"，这里只需要表达"我签发过这个路径、且还没过期"，
    根本不需要身份。HMAC 更小更简单，也绕开了 JWT 的算法混淆（alg=none）
    那一类历史漏洞。验签用 `hmac.compare_digest` 做常数时间比较，
    避免通过响应时间差逐字节猜签名。

为什么不把签名密钥直接用 jwt_secret：
    混入一个固定盐派生独立密钥。这样"媒体签名"和"登录 token"两套签名
    即使一套被攻破也不会直接波及另一套（域分离，domain separation）。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from app.config import settings

# 域分离盐：换这个值等于让所有已签发的媒体 URL 立刻失效
_SALT = b"webpilot::media-url::v1"

# 签名有效期。为什么不写死在这里：想临时缩短（比如做安全演示）只改 .env 就行。
# 选 1 小时作默认值的理由：
# - 太短（如 5 分钟）会让"打开任务页看了 10 分钟再滑到第 3 步截图"变成裂图
# - 太长（如 7 天）等于把凭证长期挂在浏览器历史/日志里
# - 过期不是死路：刷新页面会重新拉 trace，接口会用新签名重新生成地址
DEFAULT_TTL_SECONDS = 3600


def _key() -> bytes:
    """从 jwt_secret 派生出媒体签名专用密钥（进程内每次重新算，成本可忽略）。"""
    return hmac.new(settings.jwt_secret.encode("utf-8"), _SALT, hashlib.sha256).digest()


def _digest(task_id: str, filename: str, expires: int) -> str:
    """对被签名的内容算摘要。

    注意被签的是「路径 + 过期时间」这个整体：
    换了 task_id 或文件名签名就对不上，所以签名不能被"移植"到别的资源上
    （否则拿到一张截图的签名就能读整个目录）。
    """
    message = f"{task_id}/{filename}:{expires}".encode("utf-8")
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()[:32]


def sign_url(task_id: str, filename: str, ttl: int | None = None) -> str:
    """生成带签名的访问地址，形如 /runs/<task_id>/<file>?e=<过期时间戳>&sig=<摘要>。

    返回的是**相对路径**，与原来的 screenshot_url 保持一致 —— 前端可能部署在
    别的域名，写死 host 会出问题。

    ttl 传 None 时读配置（MEDIA_URL_TTL_SECONDS，默认 3600）。单个调用方想
    缩短有效期（比如给外部看的一次性预览）可以显式传小值。
    """
    if ttl is None:
        ttl = settings.media_url_ttl_seconds or DEFAULT_TTL_SECONDS
    expires = int(time.time()) + ttl
    sig = _digest(task_id, filename, expires)
    return f"/runs/{task_id}/{filename}?e={expires}&sig={sig}"


def verify(task_id: str, filename: str, expires: str | None, sig: str | None) -> bool:
    """校验一次访问是否被授权。任何一项不满足都返回 False，不区分原因。

    为什么不把"过期了"和"签名错了"分成两种返回值：
    对外都是拒绝，区分只对攻击者有利（能据此判断自己猜对了路径还是猜对了时间）。
    需要给人看的提示统一在调用方（media 路由）里写一句"地址无效或已过期"。
    """
    if not expires or not sig:
        return False
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        # 攻击者可能传 ?e=abc 试探，直接拒掉，不要让它冒泡成 500
        return False
    if expires_int < int(time.time()):
        return False
    return hmac.compare_digest(_digest(task_id, filename, expires_int), sig)
