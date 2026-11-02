"""浏览器会话：Agent 的"手和眼"。

职责边界（越界就会乱）：
- 只管执行动作：启动 / 跳转 / 点击 / 输入 / 滚动 / 截图 / 关闭
- 不管页面怎么翻译成文本（那是 M3 的 snapshot.py）
- 不管下一步做什么（那是 M4 的 workflow.py）

为什么用 async 版 Playwright：
FastAPI 和 LlamaIndex Workflows 都是 async 环境，同步版 Playwright 不能跑在
asyncio 事件循环里（会直接报错），用同步版就得把每次动作丢线程池，多一层复杂度。
现在一次选对，后面不用返工。

⚠️ async 不阻塞的前提：函数里只能 await 异步操作。
如果在这里调用同步阻塞代码（time.sleep / requests / sync Playwright），
整个事件循环会被卡住，所有请求一起排队——那才是真正的"async 堵塞"。
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from app.config import PROJECT_ROOT, settings

# 单个动作超时（毫秒）：等元素出现、等点击完成
ACTION_TIMEOUT_MS = 10_000
# 页面加载超时（毫秒）
NAV_TIMEOUT_MS = 20_000
# 截图目录：放在项目内的英文子目录，避开中文路径给工具链带来的麻烦
SCREENSHOT_DIR = PROJECT_ROOT / "runs" / "_debug"


def _proxy_config() -> dict | None:
    """解析浏览器代理配置。返回 None 表示直连。

    ⚠️ 为什么必须显式传（真实踩到的第一个硬阻塞）：
    Chromium **不读** HTTP_PROXY / HTTPS_PROXY 环境变量，它读操作系统的代理设置。
    很多代理工具（VPN、加速器、公司客户端）只设环境变量、不改系统设置，
    于是出现一个极具误导性的现象：

        终端里 curl 打开目标站点只要 3 秒，
        Agent 的浏览器 goto 同一个地址，20 秒超时。

    看起来像"站点挂了"或"网络不稳定"，实际上是**浏览器根本没走代理**。
    这个坑的排查成本很高，因为两边用的是同一台机器、同一个网络。

    bypass 也是必须的：
    一旦配上代理，连 http://127.0.0.1:8000/testsite 这种本地地址也会被代理拦下，
    白名单里的本地测试站反而访问不了了。所以必须把本地地址排除在外。

    配置优先级：settings.browser_proxy（.env 显式指定）> 环境变量 > 直连。
    填 "direct" 表示强制直连，用于"代理坏了但本地测试站还要跑"的情况。
    """
    raw = settings.browser_proxy
    if raw == "direct":
        return None
    if not raw:
        # 大小写都查：不同工具设的大小写不一样（curl 认小写，很多 Java 工具认大写）
        raw = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
            or ""
        ).strip()
    if not raw:
        return None
    return {"server": raw, "bypass": "127.0.0.1,localhost,::1"}


class BrowserSession:
    """一次任务一个实例：start() 到 close() 之间可以反复执行动作。

    两种用法：
        # 1) 自己起浏览器（单进程模式、脚本、测试）
        async with BrowserSession() as session: ...

        # 2) 借用一个已有的浏览器（arq worker 里，多个任务共用同一个 Chromium 进程）
        async with BrowserSession(browser=shared_browser) as session: ...

    为什么支持第 2 种（P3-M3 的决策，用户拍板）：
    每次 launch 一个 Chromium 要 1~4 秒，而任务本身常常只要 2~10 秒 ——
    冷启动占了可观比例，P7 算成本口径时这是纯浪费。
    共用 Browser、每任务一个 Context 之后：启动只发生一次，
    而隔离性仍然成立（cookie / localStorage / 缓存都在 context 级别）。
    """

    def __init__(
        self,
        headless: bool = True,
        *,
        browser: Browser | None = None,
        storage_state: dict | None = None,
    ) -> None:
        self._headless = headless
        # 非 None = 借来的浏览器，生命周期不归我管（close 时不能关它，
        # 否则会把同进程里其它正在跑的任务一起搞死）
        self._shared_browser = browser
        # 登录态（P5b）：Playwright 原生的 storage_state 结构（cookies + localStorage）。
        # 为什么在 new_context 时注入而不是登录完再塞 cookie：
        # 页面一加载就会读 cookie 决定"渲染账户页还是弹回登录页"。
        # 先开页面再补 cookie，第一次快照已经是"未登录"的样子，
        # 模型会据此规划去登录——那正是我们想避免的行为。
        self._storage_state = storage_state
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.steps: int = 0  # 已执行动作数，用来观察"这个任务走了几步"

    async def start(self) -> None:
        """准备浏览器 → 建隔离上下文 → 开一个标签页。"""
        if self._shared_browser is not None:
            # 借用模式：playwright 和 browser 都已经在跑，这边只开新上下文
            self._browser = self._shared_browser
        else:
            self._pw = await async_playwright().start()

            # 代理要传给 launch 而不是 new_context：
            # 代理是浏览器进程级别的设置，context 级别改不了网络出口。
            launch_kwargs: dict = {"headless": self._headless}
            proxy = _proxy_config()
            if proxy is not None:
                launch_kwargs["proxy"] = proxy
            self._browser = await self._pw.chromium.launch(**launch_kwargs)

        # 每个任务一个新 context：cookie / 本地存储天然隔离，不同任务不会串号。
        # storage_state 有值时就带身份开局（P5b 保存过的登录态）。
        context_kwargs: dict = {"viewport": {"width": 1280, "height": 800}}
        if self._storage_state:
            context_kwargs["storage_state"] = self._storage_state
        self._context = await self._browser.new_context(**context_kwargs)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    async def export_storage_state(self) -> dict:
        """把当前上下文的 cookie + localStorage 导出成 Playwright 的 storage_state。

        用途（P5b）：用户在"保存登录态"里登录一次之后，把这份东西加密存起来，
        以后任务开局直接注入，模型就不用（也不该）再填密码。

        ⚠️ 这份东西等价于"那个站点上的登录凭证"：拿到它的人不需要密码就能是那个用户。
        所以调用方必须立刻加密落库，**绝不能写日志、不能进 trace、不能回给前端**。
        """
        assert self._context is not None, "请先 await start()"
        return await self._context.storage_state()

    def _assert_host(self, url: str) -> None:
        """白名单闸门：只允许访问配置里的域名。

        这是第二道闸（第一道在 M4 的动作校验）：防模型乱跑，也防以后新增的调用方绕过校验。
        """
        host = (urlparse(url).hostname or "").lower()
        if host not in settings.allowed_hosts:
            raise PermissionError(f"域名不在白名单：{host}；允许：{settings.allowed_hosts}")

    async def goto(self, url: str) -> None:
        """打开页面。用 domcontentloaded 而不是 networkidle：
        后者遇到长轮询/懒加载页面可能一直等不到"网络空闲"，把任务卡死。
        """
        self._assert_host(url)
        assert self.page is not None, "请先 await start()"
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        """点击元素。先等它可见，避免点到隐藏元素或被遮挡的元素。"""
        assert self.page is not None, "请先 await start()"
        locator = self.page.locator(selector).first
        await locator.wait_for(state="visible")
        await locator.click()
        self.steps += 1

    async def type_text(self, selector: str, text: str) -> None:
        """向输入框写文本。fill 会先清空原内容，避免和旧值拼在一起。"""
        assert self.page is not None, "请先 await start()"
        locator = self.page.locator(selector).first
        await locator.wait_for(state="visible")
        await locator.fill(text)
        self.steps += 1

    async def select_option(self, selector: str, option: str) -> None:
        """选下拉框。

        用 label=（可见文字）而不是 value=：模型在快照里看到的是「福州」，
        它不知道底下值叫 fuzhou——让模型猜 value 是纯自找麻烦。
        """
        assert self.page is not None, "请先 await start()"
        locator = self.page.locator(selector).first
        await locator.wait_for(state="visible")
        await locator.select_option(label=option)
        self.steps += 1

    async def scroll(self, direction: str = "down", amount: int = 1) -> None:
        """滚动页面。一次滚一屏（800px），滚完等 300ms 给渲染/懒加载留时间。"""
        assert self.page is not None, "请先 await start()"
        delta = 800 * amount * (1 if direction == "down" else -1)
        await self.page.mouse.wheel(0, delta)
        await self.page.wait_for_timeout(300)
        self.steps += 1

    async def settle(self, ms: int = 400) -> None:
        """让页面自己稳定一下，不做任何动作。

        为什么需要（真实站点踩出来的）：
        换页那一瞬间，浏览器会**先把旧文档清空**，新文档还没画出来。
        这时候截图是纯白的、快照里元素数是 0、正文是空字符串。
        我们没有检测这个状态，把空快照喂给了模型，模型就只能开始编元素编号。

        注意它不是"等待网络空闲"（networkidle 在长轮询页面上会永远等不到），
        只是给渲染一个固定的缓冲窗口——所以必须短，且调用方要有次数上限。
        """
        assert self.page is not None, "请先 await start()"
        await self.page.wait_for_timeout(ms)

    async def screenshot(self, name: str) -> Path:
        """截图并返回文件路径。文件名用英文，方便后面按名字引用。"""
        assert self.page is not None, "请先 await start()"
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name}.png"
        await self.page.screenshot(path=str(path))
        return path

    async def screenshot_to(self, path: Path) -> Path:
        """截到指定路径。

        和 screenshot() 的分工：这个是"任务产物"（runs/<task_id>/step_3.png，要长期留着回放），
        那个是"临时调试"（runs/_debug/，随时可以删）。
        """
        assert self.page is not None, "请先 await start()"
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path))
        return path

    async def close(self) -> None:
        """按 context → browser → playwright 的顺序关闭。

        每步都单独 try：某一步失败不能挡住后面的清理，否则浏览器进程会残留在后台。

        ⚠️ 借用模式下**只关自己的 context**：browser 是同进程里所有任务共用的，
        谁关谁把别人的活干死。这是共享 Browser 方案唯一的真正风险点，
        所以把"归谁关"显式写在构造参数里，而不是靠调用方自觉。
        """
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._shared_browser is None:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
        self._context = None
        self._browser = None
        self.page = None

    async def __aenter__(self) -> "BrowserSession":
        """支持 async with：进入时自动启动。"""
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """退出时自动关闭，异常也一样，保证不泄漏浏览器进程。"""
        await self.close()
