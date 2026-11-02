"""生成 README 用的界面截图（P7 交付物的一部分）。

它做两件事，顺序不能反：
1. **先造数据**：注册一个演示账号，提交两类任务并等它们到位——
   一条跑完（有结论 + 完整轨迹），一条停在待审批（有审批卡）。
   没有数据的面板截出来是空壳，README 里等于没截。
2. **再截图**：用 Playwright 打开真实页面，把登录态塞进 localStorage
   （前端把 token 存在 `webpilot_token`，见 frontend/src/api.ts），
   按面板逐个截图，输出到 assets/screenshots/。

为什么不用本机浏览器手截：
- 要重复做（改前端就得重截），手动必然漂移；
- 设备像素比 2 + 固定视口 1440，README 里缩放后文字才不糊。

跑法（栈先起来：`docker compose up -d`）：
    <venv python> scripts/make_screenshots.py
    <venv python> scripts/make_screenshots.py --keep   # 保留演示账号，方便自己再截

⚠️ 任务的 start_url 必须是 `http://api:8000/testsite/...`：
导航是 worker 容器里的 Chromium 干的，它的 127.0.0.1 不是 api（踩坑日志第 51 条）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "assets" / "screenshots"
# 人访问的入口（截图和调 API 都走它，顺带验证 nginx 反代）
BASE = "http://localhost:8080"
# agent 的浏览器在 worker 容器里，必须用服务名
TESTSITE = "http://api:8000/testsite/index.html"
DEMO_EMAIL = "demo@webpilot.dev"
# 密码不写死在仓库里：演示账号跑在本地 compose 上，用环境变量覆盖即可，
# 默认值只是"第一次跑能跑通"，不是任何环境的真实凭证
DEMO_PASSWORD = os.environ.get("WEBPILOT_DEMO_PASSWORD", "webpilot-demo-2026")

WAIT_LIMIT = 240


def _post(path: str, payload: dict, token: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _get(path: str, token: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"} if token else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def login_or_register() -> str:
    """注册演示账号；已存在就登录。返回 token。"""
    code, body = _post("/api/v1/auth/register", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    if code in (200, 201):
        return body.get("token", "")
    # 409/400 = 账号已存在
    code, body = _post("/api/v1/auth/login", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    if code != 200:
        raise SystemExit(f"登录失败：HTTP {code} {body}")
    return body.get("token", "")


def wait_task(task_id: str, token: str, want: tuple[str, ...]) -> dict:
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        code, meta = _get(f"/api/v1/tasks/{task_id}", token)
        if code == 200 and meta.get("status") in want:
            return meta
        time.sleep(1.5)
    return {}


def build_data(token: str) -> tuple[str, str]:
    """造两条任务：一条跑完（有轨迹），一条停在待审批。返回 (跑完的, 待审批的)。"""
    done_id = pending_id = ""

    # 任务 1：能跑完、答案好看、轨迹有 2~3 步（README 第一张图就靠它）
    code, body = _post(
        "/api/v1/tasks",
        {
            "goal": "从首页进入产品列表，提取所有产品名称",
            "start_url": TESTSITE,
            "require_approval": False,
        },
        token,
    )
    if code == 200:
        done_id = body.get("task_id", "")
        meta = wait_task(done_id, token, ("success", "failed", "error", "nav_failed", "timeout"))
        print(f"  演示任务 {done_id} → {meta.get('status')}，{meta.get('steps')} 步")
    else:
        print(f"  提交演示任务失败：HTTP {code} {body}")

    # 任务 2：往密码框里填内容 → 触发审批闸门，界面上才会出现「需要你确认」卡片
    code, body = _post(
        "/api/v1/tasks",
        {
            "goal": "打开登录页，在密码框里输入密码，然后点击登录按钮",
            "start_url": f"{TESTSITE.replace('index.html', 'login.html')}",
            "require_approval": True,
        },
        token,
    )
    if code == 200:
        pending_id = body.get("task_id", "")
        meta = wait_task(pending_id, token, ("waiting_approval", "success", "failed", "error", "timeout"))
        print(f"  审批任务 {pending_id} → {meta.get('status')}（期待 waiting_approval）")
    else:
        print(f"  提交审批任务失败：HTTP {code} {body}")

    return done_id, pending_id


async def shoot(token: str, done_id: str, pending_id: str) -> list[Path]:
    from playwright.async_api import async_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1100}, device_scale_factor=2
        )
        # 登录态直接写 localStorage：省掉登录表单那几步，截图内容才是重点
        await page.add_init_script(
            f"localStorage.setItem('webpilot_token', {json.dumps(token)});"
            f"localStorage.setItem('webpilot_email', {json.dumps(DEMO_EMAIL)});"
        )
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        # 点「我的任务」里那条已完成的任务，让右列加载结论 + 轨迹。
        # ⚠️ 选择器必须用 .my-task-item：summary 里的「刷新」按钮也是 button，
        # 用 .panel.my-tasks button 会先命中它（DOM 顺序在前），点完什么都没变。
        async def open_item(status_text: str) -> bool:
            try:
                loc = page.locator(".panel.my-tasks .my-task-item").filter(has_text=status_text).first
                await loc.click(timeout=8000)
                await page.wait_for_timeout(3500)
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"  点开「{status_text}」任务失败：{type(exc).__name__}")
                return False

        if done_id:
            await open_item("已完成")
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)

        async def snap(name: str, selector: str | None = None, full: bool = False) -> None:
            path = OUT_DIR / name
            if selector:
                loc = page.locator(selector).first
                if await loc.count() == 0:
                    print(f"  跳过 {name}：找不到 {selector}")
                    return
                await loc.scroll_into_view_if_needed()
                await page.wait_for_timeout(400)
                await loc.screenshot(path=path)
            else:
                await page.screenshot(path=path, full_page=full)
            saved.append(path)
            print(f"  ✓ {name}")

        # 首屏总览：任务表单 + 我的任务 + 结论 + 轨迹同框
        await snap("01-overview.png")
        # 单面板特写：README 里两列并排，原图整页缩下去文字会糊
        await snap("02-trace.png", ".panel.trace")
        await snap("03-stats.png", ".panel.stats")
        # 审批卡：切到待审批那条任务才会渲染（ApprovalCard 只在当前任务 waiting_approval 时出现）
        if pending_id and await open_item("待审批"):
            await snap("04-approval.png", ".panel.approval")

        await browser.close()
    return saved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留演示账号（默认保留，加这个参数只是语义提醒）")
    parser.parse_args()

    print("1/2 造演示数据…")
    token = login_or_register()
    print(f"  演示账号就绪：{DEMO_EMAIL}")
    done_id, pending_id = build_data(token)

    print("2/2 截图…")
    saved = asyncio.run(shoot(token, done_id, pending_id))
    print(f"\n完成：{len(saved)} 张 → {OUT_DIR.relative_to(PROJECT_ROOT)}")
    for p in saved:
        print(f"  - {p.name}")
    if not pending_id:
        print("提示：没有拿到待审批任务，06-approval.png 没截到（不影响其他图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
