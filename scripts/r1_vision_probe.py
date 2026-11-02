"""R1 预检：视觉模型能不能读懂页面截图？（P6-实施计划 第十一节 R1）

它是什么：一次性的可行性实验脚本，不是正式功能代码。
在系统里演的角色：P6a「视觉兜底」开工前，先用真模型验证这条链路——
    截图 → base64 → 智谱视觉模型 → 文字描述
  如果模型连截图里的按钮/文字都读不出来，A2（vision.py）就不必写了。

实验设计（对应计划里的两个「技术地基」疑问）：
  1. 常规页面（login.html）：验证模型能否读出中文 UI 文本——
     这是「快照失败时拿截图当降级输入」的底气。
  2. canvas 页面（脚本临时生成）：canvas 画的图形在 DOM 快照里是
     一个黑盒 <canvas>，只有截图能看出里面画了什么——这是视觉兜底
     真正要救的场景。生成时埋了几个「标准答案」关键词，模型答对
     才算 PASS，不靠人眼主观判断。

模型名不写死：按候选列表依次试（GLM 视觉系列免费档），第一个能通
过的就是后续 A2 要用的 model 名。
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "runs" / "r1_probe"

# 候选视觉模型（2026-10-28 搜索核实的免费档），依次试到通为止
CANDIDATE_MODELS = ["glm-4.6v-flash", "glm-4v-flash", "glm-4.1v-thinking-flash"]

# canvas 实验页埋的「标准答案」：模型回答里必须出现这些词才算读懂
CANVAS_ANSWER_KEYWORDS = ["导出", "12"]


def load_api_key() -> str:
    """直接解析项目根 .env 拿 LLM_API_KEY。

    为什么不走 app.config：预检脚本应该脱离后端进程独立可跑，
    只依赖一个 key，不该为了读配置把整个 Settings 拖起来。
    """
    env_path = ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("LLM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("`.env` 里没有 LLM_API_KEY")


def make_canvas_page() -> Path:
    """临时生成一个 canvas 实验页（用完保留在 runs/r1_probe 供人查看）。

    里面画：一个「导出报表」按钮形状、一个大数字 12、一个柱状图。
    这些内容 DOM 快照完全看不见（canvas 对 DOM 是黑盒），
    所以模型能答出来 = 只有视觉路线能提供这个信息。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    page = OUT_DIR / "canvas_page.html"
    page.write_text(
        """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>canvas 实验页</title></head>
<body>
<canvas id="c" width="800" height="400"></canvas>
<script>
  const ctx = document.getElementById('c').getContext('2d');
  // 背景
  ctx.fillStyle = '#f5f6fa'; ctx.fillRect(0, 0, 800, 400);
  // 一个「导出报表」按钮（圆角矩形 + 文字）
  ctx.fillStyle = '#409eff';
  ctx.beginPath(); ctx.roundRect(60, 60, 160, 48, 8); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.font = '20px sans-serif';
  ctx.fillText('导出报表', 90, 92);
  // 大数字 12（今日任务数）
  ctx.fillStyle = '#303133'; ctx.font = 'bold 72px sans-serif';
  ctx.fillText('12', 60, 210);
  ctx.font = '16px sans-serif'; ctx.fillText('今日任务', 60, 240);
  // 简易柱状图
  ctx.fillStyle = '#67c23a';
  [40, 90, 60, 120, 80].forEach((h, i) => ctx.fillRect(300 + i * 70, 320 - h, 40, h));
</script>
</body></html>""",
        encoding="utf-8",
    )
    return page


async def take_screenshots() -> tuple[Path, Path]:
    """用 async Playwright 截两张图（和 A1 之后正式代码同一套浏览器层）。"""
    from playwright.async_api import async_playwright

    canvas_page = make_canvas_page()
    login_png = OUT_DIR / "login.png"
    canvas_png = OUT_DIR / "canvas.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1024, "height": 640})
        # 图1：常规登录页（中文文本丰富，验证 OCR 式读图）
        await page.goto((ROOT / "testsite" / "login.html").as_uri())
        await page.screenshot(path=str(login_png))
        # 图2：canvas 实验页（DOM 黑盒，验证「只有视觉能看见」）
        await page.goto(canvas_page.as_uri())
        await page.screenshot(path=str(canvas_png))
        await browser.close()
    return login_png, canvas_png


def ask_vision(model: str, api_key: str, image_path: Path, question: str) -> str:
    """调智谱 OpenAI 兼容接口问一张图。返回模型回答文本；失败抛异常。

    两个防御都来自预检实跑踩的坑：
    1. 免费档会甩 429——退避重试（5s/15s/30s），A2 的 vision.py 照抄；
    2. 每次重试**新建 Request 对象**——曾复用同一对象重发，智谱报
       "Request method 'GET' not supported"（本地实验证明 urllib 复用
       仍是 POST，疑似网关对连续 429 后重放连接的行为，未深究；
       每次新建就不再出现，属于"修法有效即采纳"的防御）。
    """
    import time

    img_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                        {"type": "text", "text": question},
                    ],
                }
            ],
        }
    ).encode("utf-8")

    def _one_request() -> urllib.request.Request:
        # 每次 retry 都造新 Request：不复用，见 docstring 第 2 条
        return urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

    last_exc: Exception | None = None
    # 免费档限流很紧：预检实跑发现 5/15/30s 不够用（实验 2 三次全 429），
    # 拉大到 15/45/90s——视觉兜底本来就不是高频路径，慢点无所谓
    for attempt, wait in enumerate((15, 45, 90), start=1):
        try:
            with urllib.request.urlopen(_one_request(), timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # 智谱偶尔 200 + 错误体（比如 {"error": {...}}）——
            # 直接 KeyError 会让排障抓瞎，先打出来
            if "choices" not in data:
                print(f"  [异常响应体] {json.dumps(data, ensure_ascii=False)[:300]}")
                last_exc = RuntimeError(f"响应缺 choices：{data}")
                time.sleep(wait)
                continue
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise  # 非 429 的错（401/404/参数错）重试没意义，直接抛
            last_exc = exc
            print(f"  [429 退避] 第 {attempt} 次，等 {wait}s 后重试…")
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]  # 三次都是 429，放弃


async def run_experiments() -> int:
    """截图 → 探模型 → 两个实验 → 打印结论。返回进程退出码。"""
    api_key = load_api_key()
    print("== 截图中（async Playwright，和正式代码同一套浏览器层）==")
    login_png, canvas_png = await take_screenshots()
    print(f"截图已存：{login_png} / {canvas_png}")

    # 先探出哪个视觉模型名能用（预检的副产品：A2 直接用它）
    working_model = None
    for model in CANDIDATE_MODELS:
        try:
            ask_vision(model, api_key, login_png, "你好，请只回答：收到")
            working_model = model
            print(f"\n[模型] 可用：{model}")
            break
        except Exception as exc:  # 404/无权限等都吞掉，试下一个
            print(f"[模型] {model} 不可用：{exc}")
    if working_model is None:
        print("\n结论：三个候选视觉模型都调不通，视觉兜底需要换服务商。FAIL")
        return 1

    # 实验 1：常规登录页——验证模型能读出中文 UI 文本
    print("\n== 实验 1：常规页面（login.html）==")
    answer = ask_vision(
        working_model,
        api_key,
        login_png,
        "这是一个网页截图。请列出页面上所有按钮的文字、输入框的提示文字，"
        "以及任何账号/密码相关的提示信息。用中文回答。",
    )
    login_pass = "登录" in answer and "账号" in answer
    print(f"模型回答：\n{answer}")
    print(f"判定：{'PASS' if login_pass else 'FAIL'}（要求答案包含「登录」和「账号」）")

    # 实验 2：canvas 页——验证「DOM 黑盒里只有视觉能看见」的核心场景
    print("\n== 实验 2：canvas 页（DOM 快照是黑盒）==")
    answer = ask_vision(
        working_model,
        api_key,
        canvas_png,
        "这张图里画了哪些图形和文字？请说出按钮上的文字、出现的数字。用中文回答。",
    )
    hit = [kw for kw in CANVAS_ANSWER_KEYWORDS if kw in answer]
    canvas_pass = len(hit) == len(CANVAS_ANSWER_KEYWORDS)
    print(f"模型回答：\n{answer}")
    print(
        f"判定：{'PASS' if canvas_pass else 'FAIL'}"
        f"（命中关键词 {hit} / 要求 {CANVAS_ANSWER_KEYWORDS}）"
    )

    print("\n== R1 预检结论 ==")
    print(f"视觉模型：{working_model}")
    print(f"常规页读图：{'PASS' if login_pass else 'FAIL'}")
    print(f"canvas 读图：{'PASS' if canvas_pass else 'FAIL'}")
    ok = login_pass and canvas_pass
    print(f"总体：{'视觉兜底路线可行，A2 可以开工' if ok else '路线存疑，先别开工 A2'}")
    return 0 if ok else 1


def main() -> int:
    import asyncio

    return asyncio.run(run_experiments())


if __name__ == "__main__":
    sys.exit(main())
