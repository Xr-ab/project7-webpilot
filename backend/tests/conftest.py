"""pytest 公共装置（fixture）。

只放"多个测试文件都要用的东西"：一个能用的浏览器会话、一个打开本地测试页的快捷函数。
"""
from __future__ import annotations

import pytest

from app.browser.session import BrowserSession
from app.config import PROJECT_ROOT

# 测试站目录。测试里直接用 file:// 打开，不起服务——
# 这样测的是"快照逻辑"本身，不受 HTTP 服务是否在跑的干扰。
TESTSITE = PROJECT_ROOT / "testsite"

# 所有测试页。index 是首页，其余五页各代表一类交互。
#
# P5b 加了 login.html（表单型登录页）。它必须进这个清单：
# 快照质量直接决定 Agent 能不能认出"这是账号框、那是密码框"，
# 而 browser/login.py 还要在这个页面上打标定位——快照错了，登录也就失败了。
#
# ⚠️ account.html 故意**不在**这里：它靠 cookie 决定"显示订单还是弹回登录页"，
# 而 file:// 协议下 cookie 不生效，一打开就 location.replace 到 login.html。
# 放进来的话，测的其实是登录页，而 url 断言还会因为跳转而误报。
# 它的验收放在 scripts/check_credentials.py 里跑（走真实 http 服务）。
ALL_PAGES = [
    "index.html",
    "list.html",
    "form.html",
    "search.html",
    "detail.html",
    "detail2.html",
    "login.html",
]


@pytest.fixture
async def session():
    """一个已经启动的浏览器会话，测试结束自动关闭（async with 保证不泄漏进程）。"""
    async with BrowserSession() as browser_session:
        yield browser_session


async def open_page(session: BrowserSession, name: str) -> None:
    """用 file:// 打开测试站里的某个页面。

    这里刻意绕过了 session.goto（它带域名白名单，file:// 没有 hostname 过不了闸）：
    白名单本身由 M4 的动作校验负责，不该在这里重复测。
    """
    assert session.page is not None, "session 未启动"
    await session.page.goto((TESTSITE / name).as_uri(), wait_until="domcontentloaded")
