# WebPilot 后端镜像（API 与 worker 共用一份，只靠 command 区分）
#
# 为什么 API 和 worker 共用一个镜像：
# 两者的代码依赖完全一样（同一个 app/ 包），差别只在启动命令。
# 拆成两个镜像的唯一效果是"同一份代码存两遍、构建两遍"，没有收益。
#
# ⚠️ 为什么必须用 slim(glibc) 而不是 alpine：
# Playwright 的 Chromium 是一堆预编译 .so，alpine 的 musl libc 跑不了；
# 官方镜像也是 Debian 基底。想省体积只能靠多阶段清理，不能换 alpine。
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # ASR 模型（faster-whisper small，464MB）下载到固定路径，
    # 这样 compose 能把它挂成卷——否则每次重建容器都要重新下载一遍
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

# ---------------------------------------------------------------------------
# 层序是刻意排的：越慢、越不容易变的步骤越靠前。
# 第一次构建实测 20 分钟，其中 apt 装 Chromium 依赖就占了大半；
# 如果把它排在 pip 装依赖之后，那么以后改任何一个 Python 依赖
# 都会连带把它重跑一遍 —— 每次迭代多等 15 分钟。
# ---------------------------------------------------------------------------

# 第一层：只装 playwright 这个包本身（几 MB），为了拿到 CLI。
# 版本号要和 requirements.txt 里的 pin 一致，否则下一层白装。
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir "playwright==1.64.0"

# 第二层：浏览器 + 系统依赖。这一层是全镜像最慢的一步（apt 装一百多个包），
# 但它只跟 playwright 版本有关，改业务代码不会让它失效。
# 章程 R3 记过这个坑：容器里没装依赖时，报错是"浏览器可执行文件不存在"，
# 看着像代码问题，其实是镜像少装了一层。
RUN playwright install --with-deps chromium

# 第三层：torch 单独先装，且从 PyTorch 的 **CPU 专用源**装。
# 为什么：Linux 上 PyPI 的 torch 轮子捆绑一整套 CUDA 库（nvidia-cudnn 等），
# 下载 2.5GB+、解压后更大，而本项目所有推理（bge-small-zh 向量化、whisper）
# 都是 CPU 跑，那些库一个字节都用不上。
# 为什么要 || 兜底：download.pytorch.org 国内经常连不上，连不上就退回 PyPI ——
# 慢、镜像大，但至少能构建成功，不至于让人卡在第一步。
# （pip 的版本比较里 "2.14.1+cpu" 满足 "==2.14.1"，所以下一步
#   pip install -r requirements.txt 不会把它覆盖成 CUDA 版）
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.14.1" \
 || pip install --no-cache-dir "torch==2.14.1"

# 第四层：其余全部 Python 依赖
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 业务代码 + 本地测试站 + 检查脚本。
# 为什么测试站也要进镜像：app/main.py 把 PROJECT_ROOT/testsite 挂成了静态目录，
# 而 config.PROJECT_ROOT 是按"config.py 往上三层"算的 ——
# 代码在 /app/backend/app/config.py 时，项目根就是 /app，
# 所以 testsite 必须落在 /app/testsite，少拷一次测试站就会 404。
# 为什么 scripts/ 也进镜像：check_* 系列要在容器里跑（它们直连数据库、
# 直读 runs/ 目录，从宿主机跑会连错库）——BASE=127.0.0.1:8000 在 api
# 容器内部恰好就是自己，exec 进来就能用，不用改一行代码。
COPY backend/ /app/backend/
COPY testsite/ /app/testsite/
COPY scripts/ /app/scripts/

WORKDIR /app/backend

EXPOSE 8000

# 默认命令 = 起 API，并在启动前把迁移跑完（幂等，重复执行无害）。
# worker 在 compose 里用 command 覆盖成 arq，不跑迁移——
# 迁移只该有一个执行者，两个容器同时 upgrade 会抢 DDL 锁。
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
