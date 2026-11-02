"""FastAPI 入口：把各个零件装成一个可运行的应用。

它在项目里的角色：
- uvicorn 启动时加载的对象就是这里的 `app`（命令里的 app.main:app）
- 所有对外接口最终都注册到这个 `app` 上：
  · M1：只有 /health
  · M2：app.mount 把本地测试站挂成静态目录
  · M5：include_router 把任务接口整包并进来
  · M4 收尾：runs/ 不再裸挂静态目录，改为带签名的媒体路由（app/api/media.py）
  · P2：include_router 把语音接口并进来 + lifespan 里预热 ASR 模型
- 业务逻辑不写在这里，这里只负责"装配"：建实例、注册路由、挂目录
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.approvals import router as approvals_router
from app.api.auth import router as auth_router
from app.api.credentials import router as credentials_router
from app.api.knowledge import router as knowledge_router
from app.api.media import router as media_router
from app.api.stats import router as stats_router
from app.api.tasks import router as tasks_router
from app.api.voice import router as voice_router
from app.api.voice_ws import router as voice_ws_router
from app.config import PROJECT_ROOT, settings  # 统一配置入口 + 项目根路径
from app.db import repo
from app.voice import asr

log = logging.getLogger(__name__)

# 预热任务的引用要留在模块级。
# 为什么：asyncio 只持有对 task 的弱引用，如果这里不存一份，
# 垃圾回收可能在它跑完之前把它回收掉（官方文档明确提示要保留引用）。
# 症状是"偶尔预热不生效"，非常难复现。
_warmup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用启停钩子（FastAPI 新写法，取代旧的 @app.on_event）。

    为什么要在这里预热 ASR：
    whisper small 模型加载要 10 秒，如果不预热，用户第一次按住麦克风说话，
    等的是"10 秒加载 + 1.5 秒转写"，主观感受就是"点了没反应，以为坏了"。

    为什么用 create_task 而不是 await：
    await 会让 uvicorn 卡在启动阶段 10 秒，这期间 /health 都不通、
    前端连不上会把整个页面报成"服务未启动"。
    丢到后台跑，启动立刻返回；真有人马上就用，转写请求会自己排队等模型就绪
    （asr 里的 RLock 保证了这一点）。
    """
    global _warmup_task
    if settings.voice_enabled:
        # 关键顺序：先在主线程同步 import 重模块，再开后台线程加载模型。
        # 反过来写（直接丢线程）会遇到 import 竞态：
        # 预热线程和首个任务请求同时 import httpx2，抛 "partially initialized module"。
        # 这个错只在"刚启动 + 立刻有任务"时偶发，看起来像依赖坏了，极难排查。
        asr.preload_import()
        # to_thread：模型加载是同步阻塞的，直接 create_task(asr.warmup())
        # 需要 asr.warmup 是协程；它不是，所以包一层线程。
        _warmup_task = asyncio.create_task(asyncio.to_thread(asr.warmup))

    # P3-M2：数据库探活 + 启动补偿。
    # 上次进程崩溃/被 kill 时，DB 里会留下 queued/running 的行，它们永远不会再被推进。
    # 启动时统一改成 interrupted —— 让状态诚实，比前端看到一个永远不动的 running 好。
    # 整段包在 try 里：数据库是增强项，连不上不能拦住服务启动（否则没装 PG 的机器直接跑不起来）。
    try:
        if await repo.ping():
            fixed = await repo.mark_interrupted()
            log.info("数据库已连接；启动补偿清理了 %d 个中断任务", fixed)
        else:
            log.warning("数据库连不上：P3 双写降级为纯 JSON（任务照常可跑）")
    except Exception as exc:
        log.warning("数据库初始化异常，降级为纯 JSON：%s", exc)

    yield
    # 关闭时不用特殊处理：模型占的是进程内存，进程退出自然回收


# 应用实例：路由表、静态挂载、启动钩子都挂在它身上
app = FastAPI(title="WebPilot API", version="0.1.0", lifespan=lifespan)

# 前端开发期跑在 5173（Vite 默认端口），后端在 8000 —— 跨端口就是跨源。
# Vite 那边配了 proxy，正常不会踩到 CORS；这里再加一层，方便直接 curl / Postman 调试。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 本地测试站挂成静态目录：Agent 的白名单里只有 127.0.0.1/localhost，
# 测试站和 API 同进程，省得再起一个文件服务器。
# html=True：访问目录时自动返回 index.html
app.mount(
    "/testsite",
    StaticFiles(directory=PROJECT_ROOT / "testsite", html=True),
    name="testsite",
)

# 任务产物（runs/<task_id>/step_3.png）的访问从 StaticFiles 换成了带签名的路由：
# 原来的裸挂载让 /runs/<task_id>/meta.json 无需任何凭证就能读，直接绕过 M4 归属隔离。
# 见 app/api/media.py 的模块说明。
app.include_router(media_router)

# 任务相关接口整包合并进来（prefix 在 tasks.py 里定义）
app.include_router(tasks_router)

# 语音接口：/voice/transcribe 与 /tasks/{id}/speech（prefix 在 voice.py 里定义）
app.include_router(voice_router)

# 语音会话（P6b）：WS /api/v1/voice/ws。单独一个 router 是因为 WS 端点的
# 生命周期和 REST 完全不同（首帧认证、双向事件、可打断），混在 voice.py 里会互相干扰
app.include_router(voice_ws_router)

# 统计接口（M5b）：/api/v1/stats（prefix 在 stats.py 里定义）
app.include_router(stats_router)

# 认证接口（M4）：/api/v1/auth/register、/login（prefix 在 auth.py 里定义）
app.include_router(auth_router)

# 知识检索接口（P4-N3）：/api/v1/knowledge/search（prefix 在 knowledge.py 里定义）
app.include_router(knowledge_router)

# 审批接口（P5a-2）：/api/v1/tasks/{id}/approval
# 和 tasks_router 共用 prefix，靠完整路径区分，不会互相遮挡
app.include_router(approvals_router)

# 登录态接口（P5b）：/api/v1/credentials（保存/列出/删除）
# 它负责的是"Agent 以什么身份上网"，和任务接口是两条独立的路由树，
# 只靠 tasks 里的 credential_id 入参发生关联
app.include_router(credentials_router)


# 装饰器 = 往路由表里加一条：GET /health 交给 health() 处理
@app.get("/health")
def health() -> dict[str, str]:
    """存活检查：顺便回显当前模型名，用来确认配置真的加载进内存了。"""
    return {"status": "ok", "model": settings.llm_model}
