"""vault.py（登录态加解密）的单元测试。

它在整个测试体系里的角色：**P5b 里唯一"写错了就等于泄露"的模块**。
别的地方错是功能不对，这里错是用户的会话票被别人拿走。
所以这个文件不测"能不能用"，重点测"用错的方式会不会被拒绝"——
篡改、换密钥、非预期内容，三种都必须解不开而不是解出一堆垃圾。

不测什么：Fernet 自己的密码学正确性（那是库的责任，测了也只是复述它的文档）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.security import vault

# 一份典型的 storage_state：形状和 Playwright 导出的一致
SAMPLE = {
    "cookies": [
        {
            "name": "wp_session",
            "value": "abc123",
            "domain": "127.0.0.1",
            "path": "/",
            "expires": 1900000000.0,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}


def test_roundtrip_keeps_state_intact():
    """加密再解密要一模一样。这是唯一的正向用例。

    为什么断言整个对象而不是只看 cookie 名：
    storage_state 里任何字段丢了（httpOnly / sameSite / expires）都会让注入后的
    会话行为和原会话不同——那种"有时能登录有时不能"最难查，所以要求全等。
    """
    assert vault.decrypt(vault.encrypt(SAMPLE)) == SAMPLE


def test_ciphertext_does_not_contain_plaintext():
    """密文里不能出现明文片段。

    这条防的是"看起来加密了，其实是 base64"这种假加密：
    如果谁把实现换成 base64 或明文，其他测试（往返、篡改）可能都还能过，
    但这条会立刻红。
    """
    ciphertext = vault.encrypt(SAMPLE)
    for secret in ("wp_session", "abc123", "127.0.0.1"):
        assert secret not in ciphertext, f"密文里出现了明文片段：{secret}"


def test_same_state_encrypts_differently():
    """同一份内容加密两次，密文必须不同。

    这条测的是 IV 随机。如果 IV 固定（或用了 ECB），两份相同内容的密文会一样——
    攻击者不用解密就能看出"这两个账号存的是同一个会话"。
    Fernet 自带随机 IV，这条就是防止有人以后"优化"成自己拼 AES。
    """
    first, second = vault.encrypt(SAMPLE), vault.encrypt(SAMPLE)
    assert first != second
    assert vault.decrypt(first) == vault.decrypt(second) == SAMPLE


def test_tampered_ciphertext_is_rejected():
    """改一个字节必须解不开，而不是解出一半。

    为什么这条最重要：Fernet 是**带认证**的加密（HMAC），改一个字节会导致
    认证失败而不是"解出乱码"。少了这层认证的加密方案（纯 AES-CBC）会安静地
    解出被污染的数据，那就成了"攻击者能操纵注入进浏览器的 cookie"。
    """
    ciphertext = vault.encrypt(SAMPLE)
    # 改中间一个字符，并保证真的改动了（避免恰好改成同字符导致误判通过）
    pos = len(ciphertext) // 2
    tampered = ciphertext[:pos] + ("A" if ciphertext[pos] != "A" else "B") + ciphertext[pos + 1 :]
    assert tampered != ciphertext
    with pytest.raises(vault.VaultError):
        vault.decrypt(tampered)


def test_truncated_ciphertext_is_rejected():
    """截断也要拒绝。密文是二进制不是文本流，少一截就没有"部分解密"这回事。"""
    ciphertext = vault.encrypt(SAMPLE)
    with pytest.raises(vault.VaultError):
        vault.decrypt(ciphertext[: len(ciphertext) // 2])


def test_key_rotation_makes_old_ciphertext_unreadable(monkeypatch):
    """换掉 JWT_SECRET 之后，旧密文必须解不开。

    这不是缺陷，是"密钥是唯一真相"的必然结果，也是这条测试的意义所在：
    它把"轮换密钥 = 所有已保存登录态失效"这个代价**钉在测试里**。
    哪天有人想改成"换密钥也兼容旧的"，会看到这条测试红掉，然后必须
    在计划里正面回答"那密钥泄露了怎么办"。
    """
    ciphertext = vault.encrypt(SAMPLE)

    # 模拟运维轮换密钥。
    # 注意替换的是 `vault` 模块里读到的那个 settings 引用，而不是 settings 对象本身：
    # settings 是 frozen dataclass（配置不该在运行时被改），所以 monkeypatch.setattr
    # 直接改字段会抛 FrozenInstanceError。而 vault 是 `from app.config import settings`
    # 拿的模块级引用，替换这个引用等价于"换个密钥视角"，且不会污染其他测试。
    monkeypatch.setattr(vault, "settings", SimpleNamespace(jwt_secret="rotated-secret-for-test"))
    with pytest.raises(vault.VaultError):
        vault.decrypt(ciphertext)


def test_non_dict_payload_is_rejected():
    """能解密、但内容不是 dict → 也要拒绝。

    场景：写入侧有 bug（比如传了个 list 进去）。这时候如果 decrypt 直接返回，
    调用方会把一个 list 塞进 new_context(storage_state=...)，
    Playwright 报的错会指向浏览器层，离真正的原因很远。
    """
    # 直接绕过 encrypt 造一个"合法密文但内容错"的数据：
    # 这是唯一需要碰私有函数的用例，因为 encrypt 的入参被要求是 dict，
    # 造不出这种坏数据——而坏数据恰恰是我们要防的东西。
    payload = json.dumps([1, 2, 3]).encode("utf-8")
    bad = Fernet(vault._key()).encrypt(payload).decode("ascii")
    with pytest.raises(vault.VaultError):
        vault.decrypt(bad)
