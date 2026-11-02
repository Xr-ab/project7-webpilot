"""环境自检：一条命令回答"为什么跑不起来"（P7-D0）。

它是什么：P7 一直欠着的那个脚本——把"这个项目需要什么才能跑"变成可执行的检查。
在系统里演的角色：**排障第一入口**。README 的"跑不起来怎么办"第一句就是它。

为什么值得单独一个脚本（而不是写在 README 里）：
- 环境问题有十几项（pg/redis/key/浏览器/模型/端口/依赖），靠人一项项试，
  每次至少五分钟，而且经常漏（"pg 起来了但 .env 的端口写的是 5432"）；
- 每项的修法不一样，报"失败"没用，必须报**下一步该敲什么命令**；
- 容器化之后更明显：本地能跑 ≠ 容器能跑，同一个脚本两边都能跑才有价值。

输出约定：
    [✅] 检查项 —— 通过时附一句现状（版本号/端口/开关值）
    [⚠️] 检查项 —— 能用但要注意（比如 ASR 模型没下载，首次语音会慢 10 分钟）
    [❌] 检查项 —— 阻塞，下一行给出**直接可复制**的修法

退出码：0 = 全部通过（⚠️ 不算失败）；1 = 有 ❌。

用法：
    python scripts/doctor.py            # 全量检查
    python scripts/doctor.py --quick    # 跳过联网项（LLM/外网），只查本地
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 检查结果：(级别, 标题, 说明, 修法)
OK, WARN, FAIL = "✅", "⚠️", "❌"
results: list[tuple[str, str, str, str]] = []


def add(level: str, title: str, detail: str = "", fix: str = "") -> None:
    results.append((level, title, detail, fix))


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# ---------------------------------------------------------------- 本地项
def check_python() -> None:
    version = sys.version.split()[0]
    exe = sys.executable
    if sys.version_info < (3, 10):
        add(FAIL, "Python 版本", f"当前 {version}，需要 ≥3.10", "装 3.10+ 并用项目 venv")
        return
    # 中文路径坑（M1 踩过）：沙箱里跑中文路径下的 exe 会失败，
    # 所以正常姿势是用英文绝对路径的解释器。这里只提示，不判失败
    if "chayixing" not in exe.lower():
        add(WARN, "Python 解释器", f"{exe}", "建议用项目自己的 venv（.venv/Scripts/python.exe），解释器路径不要含中文")
    else:
        add(OK, "Python 解释器", f"{version} @ {exe}")


def check_env_file() -> "object | None":
    """读 .env 并把 settings 拿回来；失败时返回 None（后续依赖它的检查会跳过）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        add(FAIL, ".env 文件", "项目根没有 .env", "cp .env.example .env 然后填 LLM_API_KEY")
        return None

    raw = env_path.read_text(encoding="utf-8")
    if "LLM_API_KEY=" not in raw or raw.split("LLM_API_KEY=", 1)[1].split("\n", 1)[0].strip() == "":
        add(FAIL, "LLM_API_KEY", ".env 里是空的", "去 https://open.bigmodel.cn 拿 key 填进去")
        return None

    try:
        from app.config import settings

        add(
            OK,
            ".env 配置",
            f"model={settings.llm_model} 队列={'开' if settings.queue_enabled else '关'} "
            f"认证={'开' if settings.auth_enabled else '关'}",
        )
        return settings
    except Exception as exc:  # noqa: BLE001
        add(FAIL, ".env 解析", f"{type(exc).__name__}: {exc}", "检查 .env 是否有非法行（缺 = 号、引号不配对）")
        return None


async def check_postgres(settings) -> None:
    if settings is None:
        add(WARN, "PostgreSQL", "配置没读到，跳过", "")
        return
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        try:
            async def _ping() -> None:
                async with engine.connect() as conn:
                    await conn.execute(text("select 1"))

            await asyncio.wait_for(_ping(), timeout=5)
        finally:
            await engine.dispose()
        add(OK, "PostgreSQL", settings.database_url.split("@")[-1])
    except Exception as exc:  # noqa: BLE001
        add(
            FAIL,
            "PostgreSQL",
            f"{type(exc).__name__}: {str(exc)[:120]}",
            "docker start wp-postgres（没建过就：docker run -d --name wp-postgres -p 5433:5432 "
            "-e POSTGRES_USER=webpilot -e POSTGRES_PASSWORD=webpilot -e POSTGRES_DB=webpilot postgres:16）",
        )


async def check_redis(settings) -> None:
    if settings is None:
        add(WARN, "Redis", "配置没读到，跳过", "")
        return
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        try:
            await asyncio.wait_for(client.ping(), timeout=4)
        finally:
            await client.aclose()
        add(OK, "Redis", settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        add(
            FAIL,
            "Redis",
            f"{type(exc).__name__}: {str(exc)[:100]}",
            "docker start wp-redis（没建过就：docker run -d --name wp-redis -p 6380:6379 redis:7）",
        )


async def check_playwright() -> None:
    """检查 Chromium 是否装好（只看可执行文件路径，不真的启动浏览器——启动要 1~2 秒）。

    ⚠️ 这里必须用 async API：整个 main 跑在事件循环里，
    playwright 的 sync API 会直接抛 "Sync API inside the asyncio loop"。
    （第一版就写成了 sync，被自检脚本自己抓出来了。）
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            path = p.chromium.executable_path
        if path and Path(path).exists():
            add(OK, "Playwright Chromium", str(path))
        else:
            add(FAIL, "Playwright Chromium", f"路径不存在：{path}", "python -m playwright install chromium")
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "Playwright Chromium", f"{type(exc).__name__}: {str(exc)[:100]}", "pip install playwright && python -m playwright install chromium")


def check_asr_model(model_name: str) -> None:
    """ASR 模型有没有下载过（不加载，只看缓存目录）。"""
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    pattern = f"models--Systran--faster-whisper-{model_name}"
    if (hub / pattern).exists():
        add(OK, "ASR 模型", f"{model_name} 已缓存")
    else:
        add(
            WARN,
            "ASR 模型",
            f"{model_name} 未下载（首次语音会现下，464MB）",
            "想要提前下好：跑一次 scripts/check_voice_ws.py 或调一次 /voice/transcribe",
        )


def check_ports(settings) -> None:
    """API / 前端 dev server 是否在跑。没在跑不算失败——CLI 用法不需要它们。"""
    api_port = int(settings.app_port) if settings else 8000
    if port_open("127.0.0.1", api_port):
        add(OK, f"API 端口 {api_port}", "已监听")
    else:
        add(WARN, f"API 端口 {api_port}", "没在监听", "cd backend && python -m uvicorn app.main:app --port 8000")

    if port_open("127.0.0.1", 5173):
        add(OK, "前端 dev server 5173", "已监听")
    else:
        add(WARN, "前端 dev server 5173", "没在监听", "cd frontend && npm run dev")


def check_assets() -> None:
    missing = [
        name
        for name in ("testsite/index.html", "testsite/canvas.html", "backend/requirements.txt")
        if not (ROOT / name).exists()
    ]
    if missing:
        add(FAIL, "关键文件", f"缺：{', '.join(missing)}", "检查是否在项目根运行、仓库是否完整")
    else:
        add(OK, "关键文件", "测试站页面 / requirements 都在")

    try:
        probe = ROOT / "runs" / ".doctor_probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "runs/ 可写", str(ROOT / "runs"))
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "runs/ 可写", f"{type(exc).__name__}: {exc}", "检查目录权限；容器里注意卷挂载")


async def check_llm(settings) -> None:
    """真调一次模型（只花一个 token）。429 也算可用——说明网络通、key 有效。"""
    if settings is None:
        add(WARN, "LLM 连通性", "配置没读到，跳过", "")
        return
    import json
    import urllib.error
    import urllib.request

    body = json.dumps(
        {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
    ).encode()
    req = urllib.request.Request(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            json.loads(resp.read().decode())
        add(OK, "LLM 连通性", f"{settings.llm_model} 可达")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            add(OK, "LLM 连通性", f"{settings.llm_model} 可达（当前限流 429，重试即可）")
        elif exc.code in (401, 403):
            add(FAIL, "LLM 连通性", f"HTTP {exc.code}：key 无效或无权限", "换一个 LLM_API_KEY")
        else:
            add(FAIL, "LLM 连通性", f"HTTP {exc.code}", "检查 LLM_BASE_URL 是否是 https://open.bigmodel.cn/api/paas/v4")
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "LLM 连通性", f"{type(exc).__name__}: {str(exc)[:100]}", "检查网络/代理（BROWSER_PROXY 只影响浏览器，不影响这里）")


def check_switches(settings) -> None:
    if settings is None:
        return
    add(
        OK,
        "功能开关",
        f"语音={'开' if settings.voice_enabled else '关'} "
        f"视觉={'开' if settings.vision_enabled else '关'}"
        f"（自动兜底={'开' if settings.vision_auto_fallback else '关'}） "
        f"审批={'开' if settings.approval_enabled else '关'}",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="WebPilot 环境自检")
    parser.add_argument("--quick", action="store_true", help="跳过联网检查（LLM）")
    args = parser.parse_args()

    check_python()
    settings = check_env_file()
    await check_postgres(settings)
    await check_redis(settings)
    await check_playwright()
    if settings is not None:
        check_asr_model(settings.asr_model)
        check_ports(settings)
        check_switches(settings)
    check_assets()
    if not args.quick:
        await check_llm(settings)

    print("\nWebPilot 环境自检\n" + "=" * 60)
    for level, title, detail, fix in results:
        line = f"{level} {title}"
        if detail:
            line += f" —— {detail}"
        print(line)
        if fix:
            print(f"     → {fix}")

    failed = sum(1 for level, *_ in results if level == FAIL)
    warned = sum(1 for level, *_ in results if level == WARN)
    print("=" * 60)
    print(f"通过 {len(results) - failed - warned} 项；注意 {warned} 项；阻塞 {failed} 项")
    if failed:
        print("有阻塞项：按上面的 → 逐条修复后重跑本脚本。")
    else:
        print("环境可用。" + ("（⚠️ 项不阻塞，但建议处理）" if warned else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
