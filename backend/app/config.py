"""WebPilot 唯一配置入口。

为什么要有这个文件：
- 业务代码不直接读 os.environ：环境变量是字符串、可能缺失、名字容易散落各处
- 全部收敛到这里之后：换服务商 / 改上限只动一处；缺配置启动即报错；测试时可替换配置对象

注意：防密钥泄露不靠这个文件，靠两件事——
1. .env 进 .gitignore，不进版本库
2. 日志和报错不打印 key（本文件只构造配置对象，从不打印内容）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 用文件位置定位项目根（parents[2]：config.py -> app -> backend -> 项目根），
# 不靠当前工作目录——uvicorn 从 backend 起、脚本从根目录起，cwd 会变
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 先把 .env 读进环境变量，再 os.getenv；顺序反了读不到
load_dotenv(PROJECT_ROOT / ".env")

# HuggingFace 下载源兜底（P4 起 embedding 模型从这里拉）。
# 实测：不设它直连 huggingface.co 会 502（网络不可达），sentence-transformers
# 报 "Could not resolve the revision" 然后每任务失败一次。
# setdefault 语义：.env 或系统环境里已经设了（比如公司代理源）就尊重已有值，
# 没设才用国内镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


@dataclass(frozen=True)
class Settings:
    """配置字段清单：字段名就是类型契约，业务代码只认这个对象。

    frozen=True 的理由：配置在启动后不允许被改。
    否则某个模块半路把 max_steps 改成 50，失败复现时说不清当时用的哪个值。
    """

    llm_api_key: str
    llm_base_url: str
    llm_model: str
    max_steps: int
    task_timeout_seconds: int
    allowed_hosts: tuple[str, ...]
    app_port: int
    # 语音任务（不带网址概念）从哪页开始跑。留空 = 用 127.0.0.1:APP_PORT 拼；
    # 容器模式必须配成 http://api:8000/testsite/index.html ——
    # start_url 是给 worker 容器里的 Chromium 用的，它的 127.0.0.1 不是 api
    default_start_url: str
    # ---- 语音（P2）----
    voice_enabled: bool
    asr_model: str
    asr_language: str
    tts_voice: str
    voice_max_mb: int
    # ---- 浏览器（P2.5 走出测试站）----
    browser_proxy: str
    # ---- 存储（P3）----
    # 有默认值而不是必填：P3 之前项目不依赖数据库，任何一台机器 clone 下来
    # 不建库也能把 P1/P2 的功能跑起来。M1 只是把表结构准备好，读路径还在 JSON 上。
    database_url: str
    # ---- 队列（P3-M3）----
    # 端口 6380：理由同 DATABASE_URL 的 5433，避免和本机其它项目的 redis 撞车
    redis_url: str
    # 队列开关。关掉就退回"进程内 asyncio.create_task"（P1 的行为），
    # 留着这个开关是为了：① 排查"到底是不是队列的问题" ② 没有 redis 的机器
    queue_enabled: bool
    # 同时跑几个任务。定 3 的理由：免费档 LLM 有并发限制，
    # 且每个 Chromium 吃 200~400MB。既够演示排队，又不会打爆配额和内存。
    max_jobs: int
    # ---- 认证（P3-M4）----
    # JWT 签名密钥。给默认值是为了 clone 下来不改 .env 也能跑（本地开发语义）；
    # 任何对外部署必须改掉——密钥泄露 = 任何人可以伪造任意用户的 token。
    jwt_secret: str
    # 认证开关：关掉时全部任务接口退回 P3-M3 的行为（挂占位用户 local@webpilot.dev，
    # 不校验 token）。和 QUEUE_ENABLED 同一风格：隔离是增强项，不是硬依赖。
    # 默认开——验收标准就是"不带 token → 401"。
    auth_enabled: bool
    # token 有效期（分钟）。8 小时 ≈ 一个工作日，过期重新登录
    token_expire_minutes: int
    # 任务媒体（截图/语音）签名 URL 的有效期（秒）。
    # 为什么不是"跟 token 一样长"：token 是身份凭证，媒体签名只是"这次访问被授权过"，
    # 有效期越短越安全。1 小时足够覆盖"打开任务页慢慢往下看"的场景，
    # 过期也不影响使用——刷新页面会重新拉 trace，拿到新签名。
    media_url_ttl_seconds: int
    # ---- 审批停等（P5a）----
    # 高风险动作是否需要人工批准。默认开（默认安全）。
    # ⚠️ 批处理脚本（eval_real / stress / collect_corpus）必须显式关掉：
    # 它们假设"任务一口气跑到底"，一旦触发审批就会永远卡在 waiting_approval。
    approval_enabled: bool

    # ---- 登录限流（P5 收尾补的 M4 遗留）----
    # 同一 email 连续登录失败多少次后临时锁定。5 次是常见基线：
    # 太少会误伤"手滑输错两三次"的正常用户，太多防不了撞库。
    login_max_failures: int
    # 锁多久（秒）。15 分钟：够让脚本小孩放弃，又不至于把忘密码的人挡到天黑。
    # 为什么按 email 不按 IP：本机/内网部署大家共享出口 IP，按 IP 会把
    # 整个办公室锁一起；email 维度防的是"盯着一个账号试密码"的撞库，
    # 换 email 散打的攻击面由注册环节的校验承担——这个边界写进 security/ratelimit.py。
    login_lockout_seconds: int

    # ---- 视觉兜底（P6a）----
    # 总开关，**默认关**。关着时任何路径都不会调视觉模型——
    # 这是回归防护的硬要求（P6 计划第十节）：开着会改变 prompt，
    # 现有评测基线（eval_p1 4/5、23 条真实站点任务）的数字就全部不可比了。
    # 演示/验收时通过 .env 或任务级 vision=true 打开。
    vision_enabled: bool
    # 总开关打开后，是否按"兜底条件"自动触发（决策 1-C）。
    # 独立开关的理由：压测/回归时想开总开关测通路，但不想被自动触发干扰。
    vision_auto_fallback: bool
    # 视觉模型走智谱同一个 base_url 和 key（P6 决策 5）。
    # ⚠️ 预检实测（2026-10-28）：glm-4v-flash 几乎不撞 429；
    # 原计划的 glm-4.6v-flash 免费额度极紧（15/45/90s 三级退避全过不去），别换回去。
    vision_model: str
    # 兜底条件 1 的阈值：可交互元素少于这个数，且页面有 canvas / 大图
    vision_trigger_min_elements: int
    # 兜底条件 2：连续几步动作失败后开始看图（模型在猜，说明它看不懂页面）
    vision_trigger_max_failures: int


def _flag(name: str, default: bool) -> bool:
    """解析布尔开关。

    为什么要单独写：os.getenv 拿到的永远是字符串，
    而 "false" 是个非空字符串，直接 bool() 会得到 True——
    这是个经典坑（VOICE_ENABLED=false 反而开了）。这里显式比对字面量。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _require(name: str) -> str:
    """必填项：缺失立刻抛错，错误信息直接告诉你怎么修（fail-fast）。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}：复制 .env.example 为 .env 并填写")
    return value


def load_settings() -> Settings:
    """把环境变量解析成强类型对象。解析只发生这一次，业务代码拿到的都是成品。"""
    # 白名单：逗号分隔的字符串 -> 小写、去空的元组
    hosts_raw = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost")
    hosts = tuple(h.strip().lower() for h in hosts_raw.split(",") if h.strip())
    return Settings(
        llm_api_key=_require("LLM_API_KEY"),
        # 三个 LLM 字段都有默认值：换服务商只需改 .env，代码不动
        llm_base_url=os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").strip(),
        llm_model=os.getenv("LLM_MODEL", "glm-4-flash").strip(),
        max_steps=int(os.getenv("MAX_STEPS", "12")),
        task_timeout_seconds=int(os.getenv("TASK_TIMEOUT_SECONDS", "180")),
        allowed_hosts=hosts,
        app_port=int(os.getenv("APP_PORT", "8000")),
        # 默认留空：宿主机单进程部署拼 127.0.0.1 就对；容器模式由 compose 覆盖
        default_start_url=os.getenv("DEFAULT_START_URL", "").strip(),
        # 语音默认开启。想跳过 ASR 那 464MB 模型和 10 秒预热时关掉它，
        # 关掉后语音接口返回 503，前端会把麦克风按钮置灰。
        voice_enabled=_flag("VOICE_ENABLED", True),
        # small 是体积/准确率的甜点位：464MB，中文短句识别基本不错一个关键字。
        # 降到 base 省 300MB 但错字明显变多；升到 medium 要 1.5GB，收益不值。
        asr_model=os.getenv("ASR_MODEL", "small").strip(),
        # 写死 zh：v0.1 只服务中文指令。改成 auto 会让模型多走一步语言检测，
        # 而且短句检测很容易判错（中文被认成日语是常见现象）。
        asr_language=os.getenv("ASR_LANGUAGE", "zh").strip(),
        # 晓晓：微软最稳的通用中文女声，语速自然，适合念答案
        tts_voice=os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip(),
        # 上传音频体积上限。60 秒的 webm/opus 大约 300KB，
        # 20MB 足够容纳一次很长的口述，同时挡住误传的大文件
        voice_max_mb=int(os.getenv("VOICE_MAX_MB", "20")),
        # 浏览器代理。留空 = 自动从 HTTP(S)_PROXY 环境变量读（见 browser/session.py）。
        # 想强制直连（哪怕系统设了代理）就填 "direct"。
        browser_proxy=os.getenv("BROWSER_PROXY", "").strip(),
        # 默认指向本机 docker 起的 pg（见 P3-实施计划.md）。用 asyncpg 驱动。
        # ⚠️ 端口是 5433 不是 5432：本机 5432 已经被另一个项目的 postgres 占了，
        # 硬挤进去会连到别人的库。各项目一套独立实例，互不干扰。
        database_url=os.getenv(
            "DATABASE_URL", "postgresql+asyncpg://webpilot:webpilot@127.0.0.1:5433/webpilot"
        ).strip(),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6380/0").strip(),
        # 默认开：M3 之后队列是正常路径。连不上 redis 时 submit 会自动降级成
        # 进程内执行（见 task_runner.submit），所以默认开不会让没装 redis 的机器跑不起来。
        queue_enabled=_flag("QUEUE_ENABLED", True),
        max_jobs=int(os.getenv("MAX_JOBS", "3")),
        jwt_secret=os.getenv("JWT_SECRET", "dev-secret-change-me").strip(),
        auth_enabled=_flag("AUTH_ENABLED", True),
        token_expire_minutes=int(os.getenv("TOKEN_EXPIRE_MINUTES", "480")),
        media_url_ttl_seconds=int(os.getenv("MEDIA_URL_TTL_SECONDS", "3600")),
        # 默认开：审批是安全特性，"默认安全"比"默认方便"重要。
        # 关掉时 gate 直接放行，行为退回 P4（任务一口气跑到底）。
        approval_enabled=_flag("APPROVAL_ENABLED", True),
        login_max_failures=int(os.getenv("LOGIN_MAX_FAILURES", "5")),
        login_lockout_seconds=int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900")),
        # 视觉兜底：默认全关。P6 计划第十节的硬要求——视觉会改 prompt，
        # 默认开会让现有评测基线全部失真（且不会有任何测试红，数字悄悄偏）。
        vision_enabled=_flag("VISION_ENABLED", False),
        vision_auto_fallback=_flag("VISION_AUTO_FALLBACK", True),
        vision_model=os.getenv("VISION_MODEL", "glm-4v-flash").strip(),
        vision_trigger_min_elements=int(os.getenv("VISION_TRIGGER_MIN_ELEMENTS", "3")),
        vision_trigger_max_failures=int(os.getenv("VISION_TRIGGER_MAX_FAILURES", "2")),
    )


# 模块级单例：整个进程只加载一次，所有模块 import 到的是同一份配置
settings = load_settings()
