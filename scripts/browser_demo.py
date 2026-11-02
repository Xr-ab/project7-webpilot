"""M2 验收脚本：用代码控制浏览器走一遍完整的动作链。

前置：后端已启动（它负责提供 /testsite 静态页）
    cd backend && <venv python> -m uvicorn app.main:app --port 8000
跑法（项目根目录）
    <venv python> scripts/browser_demo.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.browser.session import BrowserSession
from app.config import settings

BASE = f"http://127.0.0.1:{settings.app_port}/testsite"


async def timed(label: str, coro) -> None:
    """执行一个动作并打印耗时，方便看出哪一步慢。"""
    started = time.perf_counter()
    await coro
    print(f"{label:<14} {time.perf_counter() - started:6.2f}s")


async def main() -> None:
    session = BrowserSession(headless=True)

    # 启动耗时单独计时：它比单个动作慢一个数量级，混在一起看会误判
    started = time.perf_counter()
    await session.start()
    print(f"{'启动浏览器':<14} {time.perf_counter() - started:6.2f}s")

    try:
        await timed("打开首页", session.goto(f"{BASE}/index.html"))
        await timed("截图 01", session.screenshot("m2-01-index"))

        await timed("点击导航", session.click("a[data-nav='list']"))
        await timed("截图 02", session.screenshot("m2-02-list"))

        await timed("输入关键词", session.type_text("#keyword", "键盘"))
        await timed("点击筛选", session.click("#search-btn"))
        await timed("截图 03", session.screenshot("m2-03-filtered"))

        # 白名单闸门：故意访问外部站点，应当被 PermissionError 拦下
        try:
            await session.goto("https://example.com")
        except PermissionError as exc:
            print("白名单拦截：", exc)

        print("最终 URL：", session.page.url)
        print("动作步数：", session.steps)
    finally:
        # 无论成功、失败还是异常，都要关掉浏览器，避免进程残留
        await session.close()
        print("浏览器已关闭")


if __name__ == "__main__":
    asyncio.run(main())
