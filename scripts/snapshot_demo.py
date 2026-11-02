"""M3 验收脚本：给测试站的每个页面拍快照，落盘成 JSON + 可读文本。

跑法（先起服务，再跑脚本）：
    1) cd backend && <venv python> -m uvicorn app.main:app --port 8000
    2) <venv python> scripts/snapshot_demo.py

    可选：<venv python> scripts/snapshot_demo.py --base http://127.0.0.1:8000/testsite

产出：
    runs/snapshots/<页面名>.json  —— 结构化快照，后面 workflow 直接拿它当输入/调试用
    runs/snapshots/<页面名>.txt   —— 渲染后的文本，就是真正要喂给模型的东西

为什么走 http 而不是 file://：
    走 http 才和真实场景一致（相对链接、白名单、静态挂载全部经过一遍）。
    走 file:// 会把 session.goto 的域名白名单绕过去，测出来的东西和线上不一样。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 让脚本能 import 到 backend/app 下的模块（脚本在 scripts/，代码在 backend/）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.browser.session import BrowserSession  # noqa: E402
from app.browser.snapshot import render_snapshot_text, take_snapshot  # noqa: E402

OUT_DIR = PROJECT_ROOT / "runs" / "snapshots"

PAGES = [
    "index.html",
    "list.html",
    "form.html",
    "search.html",
    "detail.html",
    "detail2.html",
]


def server_is_up(base: str) -> bool:
    """先探一下服务在不在，别让 Playwright 报一个看不出原因的 404/超时。"""
    try:
        with urllib.request.urlopen(base + "/index.html", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


async def main(base: str) -> int:
    if not server_is_up(base):
        print(f"服务没起：{base} 打不开。\n先执行：cd backend && <venv python> -m uvicorn app.main:app --port 8000")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with BrowserSession() as session:
        for name in PAGES:
            await session.goto(f"{base}/{name}")
            snap = await take_snapshot(session.page)
            text = render_snapshot_text(snap)

            stem = name.removesuffix(".html")
            (OUT_DIR / f"{stem}.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (OUT_DIR / f"{stem}.txt").write_text(text, encoding="utf-8")

            print("=" * 72)
            print(f"# {name}  （元素 {snap['elements_shown']}/{snap['elements_total']}）")
            print("=" * 72)
            print(text)
            print()

    print(f"已写入：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/testsite")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.base)))
