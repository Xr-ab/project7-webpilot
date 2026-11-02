"""登录态（storage_state）的对称加密（P5b）。

它在系统里的角色：**把"能登录进去"这件事从明文密码手里接过来**。

解决的两个问题，其实是一个：
1. Agent 遇到要登录的站就卡住——它没有身份。
2. 上一条发现的安全问题：模型给密码框 `type` 时，动作的 `text` 是明文密码，
   而 `approvals.action` 存的是 `Action.model_dump()` 全量字段 → 密码进了数据库。
   只要 Agent 开局就是登录态，它就**不需要**在任务里填密码，这条泄露路径从源头消失。

为什么不存密码、改存 storage_state：
- 密码是"能推出未来所有会话"的根凭证；storage_state 是"某一个会话的票"，
  过期就废，泄露面小得多
- 复用会话不需要重新登录，也就不会撞上验证码/多因素那一堆麻烦
- 代价：会话会过期，过期就得重新保存（前端要能说清这件事）

为什么用 Fernet（AES-128-CBC + HMAC，带认证）而不是自己拼：
对称加密的每一种自制方案都踩过坑——ECB 泄露结构、CBC 无认证可被篡改、
固定 IV 泄露相同前缀。Fernet 把"加密 + 认证 + 时间戳"打包好，
用错的方式基本写不出来（它连 IV 都帮你随机）。**不要手搓密码学。**

密钥来源与域分离：
`JWT_SECRET` 当主密钥，混一个固定盐派生 Fernet key——和 `storage/signing.py`
那套（媒体签名）完全同一个思路。这样三套密钥（登录 token / 媒体签名 / 凭证加密）
互不相干，一套泄露不波及另一套。

⚠️ 必须说清的代价：**换掉 `JWT_SECRET` 等于所有已保存的登录态立刻失效**
（解不开了）。这不是缺陷，是"密钥是唯一真相"的必然结果；写在这里是为了
下次有人轮换密钥时知道要提前通知用户重新保存。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

# 域分离盐：换这个值等于让所有已存凭证失效（和 signing.py 的 _SALT 同一套做法）
_SALT = b"webpilot::credential-vault::v1"


class VaultError(RuntimeError):
    """解密失败。调用方把它翻译成"这个登录态失效了，请重新保存"。"""


def _key() -> bytes:
    """从 jwt_secret 派生 Fernet key。

    两层：先 HMAC-SHA256 得到 32 字节（做域分离），再 urlsafe-base64 编码
    ——Fernet 要求的输入格式就是这个 44 字符的 base64 串。
    """
    raw = hmac.new(settings.jwt_secret.encode("utf-8"), _SALT, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw)


def encrypt(state: dict) -> str:
    """把 storage_state 加密成一串可存 Text 列的密文。

    先 json.dumps 再加密（不是对 dict 的 repr 加密）：dict 的顺序在 Python 里
    虽然稳定，但 repr 的格式是给调试看的、不是协议，跨版本可能变。
    ensure_ascii=False 让中文账号名不被转义成 \\uXXXX——它会让密文长一截，
    而且解密后得到的对象完全一样，没有坏处。
    """
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Fernet(_key()).encrypt(payload).decode("ascii")


def decrypt(ciphertext: str) -> dict:
    """解密回 storage_state。任何失败都抛 VaultError，不区分原因。

    为什么要吞掉具体异常：InvalidToken 可能是"密钥换了"、"密文被改了一个字节"、
    "密文被截断"——对调用方来说处理方式完全一样（让用户重新保存），
    区分只会让错误信息变成攻击者的探测工具。
    """
    try:
        raw = Fernet(_key()).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise VaultError("登录态无法解密（密钥已轮换或数据损坏），请重新保存") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # 能解开但解出来不是 JSON：只可能是写入侧有 bug，属于内部错误
        raise VaultError("登录态内容不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise VaultError("登录态内容格式不对")
    return data


def encrypt_text(text: str) -> str:
    """加密一个字符串（P5b-脱敏：审批动作里的密码真值）。

    复用 encrypt 的 JSON 协议包一层 {"v": ...}，而不是另写一套序列化：
    Fernet 的密文格式、密钥派生、失败语义全都继承，只会有一处实现。

    用在哪：闸门拦下"往密码框输入"后，落库的 action.text 打码成 ***，
    但批准续跑要**原样重放**这个动作——真值以密文形式跟着 action 走
    （塞在 action dict 的 _secret 键里），库里始终没有明文。
    """
    return encrypt({"v": text})


def decrypt_text(ciphertext: str) -> str:
    """encrypt_text 的逆操作。失败抛 VaultError（语义同 decrypt）。"""
    data = decrypt(ciphertext)
    value = data.get("v")
    if not isinstance(value, str):
        # 能解开但结构不对：只可能是写入侧 bug，归为内容错误
        raise VaultError("密文内容格式不对")
    return value
