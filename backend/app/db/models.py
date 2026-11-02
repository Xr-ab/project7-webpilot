"""数据库表结构：P3 的持久化地基。

它在项目里的角色：
- P1/P2 的状态都在进程内存和 JSON 文件里（`runs/<task_id>/meta.json`）。
  进程一重启，正在跑的任务状态就丢了；跨任务想统计"哪类动作最容易失败"也做不到。
- 这三张表就是那两件事的解法：`tasks` 存一次运行的终态，`task_steps` 存每步明细，
  `users` 给任务一个主人（否则谁都能查别人的任务）。

为什么建表不等于切读路径：
M1 只把结构准备好（`alembic upgrade head` 能建出来），M2 才开始双写、再切读。
这样任何一步出问题都能退回 JSON，不会出现"表还没建好，业务已经读不到数据"。

字段设计上和现有代码的对齐关系（别自己另发明一套）：
- `tasks.id` 就是现在的 `task_id`（`20261024-122916-kaek3n`），**不换数字主键**——
  截图路径、前端 URL、`runs/` 目录全依赖这个格式，换掉是无收益的连锁改动。
- `task_steps` 就是把 `trace.json` 一行一条拆开，字段名尽量和 trace 保持一致，
  这样落库代码只是"搬运"，不需要做映射翻译。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# pgvector 的向量类型。import 放在这层：models 是表结构的单一真相，
# 向量列的类型定义必须和表在一起；装不上 pgvector 包时整个 db 层都不该启动
# （比"表建出来了但向量列是 Text"更早暴露问题）。
from pgvector.sqlalchemy import Vector

# 主键类型：PostgreSQL 上是 BIGSERIAL，SQLite 上退化成 INTEGER。
# 为什么留这个兼容：P3 主库是 PostgreSQL（章程定的），但把 SQLite 的路留一条，
# 是为了"没装数据库的机器也能跑单测"和"以后想做嵌入式演示"两种情况不用改模型。
PK = BigInteger().with_variant(Integer, "sqlite")

# JSON 列：PostgreSQL 用 JSONB（可索引、查询快），其它方言退化成普通 JSON。
# plan / progress 是模型自己给的任务分解，会被整体读写，用 JSONB 存一列最省事。
JSONType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    """所有表的基类。alembic 靠 `Base.metadata` 找到全部表定义。"""


class User(Base):
    """用户。P3 之前没有这个概念，所有任务都是"无主"的。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # 只存 bcrypt 哈希，明文密码永不落库、永不写日志
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Task(Base):
    """一次 Agent 运行（= 现在的一个 `runs/<task_id>/` 目录）。"""

    __tablename__ = "tasks"

    # 沿用现有 task_id 格式，不做数字主键
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        PK, ForeignKey("users.id"), nullable=False
    )

    goal: Mapped[str] = mapped_column(Text, nullable=False)
    start_url: Mapped[str] = mapped_column(Text, nullable=False)

    # queued / running / success / error / loop / timeout / invalid_action / nav_failed / interrupted
    # 不加 Enum：status 会跟着 Agent 能力演进新增（比如 P5 的 waiting_approval），
    # 用 Enum 每次都要改迁移，收益抵不上麻烦。
    status: Mapped[str] = mapped_column(Text, nullable=False)

    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(Text, nullable=False)

    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 模型自己定的任务分解 + 已完成条目。存下来才能回答
    # "没涨分是因为机制没用，还是模型没遵守" —— 这两种结论指向完全相反的下一步。
    plan: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    progress: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    # 冗余汇总：省掉每次统计都去 SUM(task_steps)。
    # 代价是写任务终态时要一起算好，换来的是 /stats 接口不用扫全表。
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_s: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        # 列表页的默认查询：某个用户最近的任务，倒序
        Index("tasks_user_created", "user_id", "created_at"),
        # 启动补偿扫描要按 status 找遗留的 running
        Index("tasks_status", "status"),
    )


class TaskStep(Base):
    """每步明细（= `trace.json` 的一行）。这是"可追溯"和"能聚合"的核心。"""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # ⚠️ 这一列不能省，也不能改成 UNIQUE(task_id, step_no)。
    # workflow.py 的 _record_rejected() 会让"被拒绝的决策"和"重试后的决策"
    # 共用同一个 step_no（重试不推进步数）。加唯一约束的话第一次拒绝就插不进去。
    # 有了 attempt，"这个任务被拒了几次"就是一句 count，而这是失败归因的关键指标。
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thought: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 只存压缩后的短错误（prompts.short_error 的产物）；1065 字符的 Playwright
    # 原文留在截图同目录的归档里，不入库 —— 塞进 DB 只会让表膨胀且没人查
    error: Mapped[str | None] = mapped_column(Text)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elements_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screenshot: Mapped[str | None] = mapped_column(Text)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- P6a 视觉兜底留痕 ----
    # 形如 {"used": true, "model": "glm-4v-flash", "trigger": "task",
    #       "ms": 8304, "summary": "5月 4700…"}；没看图的步骤是 NULL。
    #
    # ⚠️ 这一列是 A5 验收时**被现实打出来**的（踩坑 #42）：
    # trace.json 里加字段是"只增不改"、天然兼容，但 task_steps 是**固定列**表——
    # 不给它加列，API /trace 就看不到 vision 字段，于是"磁盘上有、接口里没有"。
    # 教训：跨 JSON 和 DB 两种存储的字段，两边都得登记一次，别指望 JSON 能带过去。
    vision: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 详情页按 (task_id, step_no) 顺序取，这个索引直接覆盖
        Index("task_steps_task", "task_id", "step_no"),
    )


# ---- P4 数据闭环：知识节点 ----

# bge-small-zh-v1.5 的输出维度。改 embedding 模型必须同时改这一处 + 迁移，
# 且要重嵌全表——维度不同的向量放同一列会查出错结果（不是报错，是静默错）。
EMBED_DIM = 512


class KnowledgeNode(Base):
    """从任务里沉淀出来的一条知识。

    它在项目里的角色：
    - P3 结束时，任务跑完的产物只是"日志"——answer 躺在 tasks 表里，正文躺在
      trace.json 里，除了一次次手动翻看，产生不了第二次价值。
    - P4 把它们切片入这张表、带向量，于是"之前采集的数据里 XX 是多少"变成一次
      检索；Agent 从"单次任务工具"变成"会积累的助手"。

    为什么三种 source_type 放一张表：
    - 任务级（goal+answer）、页面级（正文）、事实级（KV）在检索时是同一件事：
      "拿一段文本找相似的"。分表只会让检索逻辑写三遍。差异全放 meta。
    """

    __tablename__ = "knowledge_nodes"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)

    # task_answer / page_text / fact —— 对应三层语料，见 P4-实施计划 §二
    source_type: Mapped[str] = mapped_column(Text, nullable=False)

    # 溯源：这条知识来自哪个任务。ondelete CASCADE：任务被删，它的知识跟着走——
    # 否则会出现"点开来源引用 404"的孤儿节点。
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )

    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 展示用短标题（任务级 = goal 截断；页面级 = 页面 title）
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 被索引的正文。任务级 = goal + 答案；页面级 = 快照纯正文
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # content 的 sha256（64 hex）。
    # 为什么需要：同一个 goal 被 eval 反复跑（同一批题多轮评测），每轮都是
    # 独立的 task_id，内容却一模一样。实测 72 条节点里只有 10 条是不同内容——
    # 不去重的话检索 top-k 会被同一知识的多份副本占满，等于 k=1。
    # 不直接用 content 建唯一索引：长文本 btree 索引会超长度限制（2704 字节）。
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # 向量列。SQLite 退化成 Text 是为了"没装 pg 的机器也能跑单测"（同 PK/JSONType 的思路）；
    # 退化后检索走不了向量，但建表、写入、读回都能测。
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBED_DIM).with_variant(Text, "sqlite")
    )

    # 事实级的 KV、页面级的步骤号、采集时间等。检索结果展示要按它出引用信息
    meta: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    # 采集者（M4 收尾）。nullable 的两个理由：
    # 1. 历史节点是在这一列存在之前入库的，回填 owner 需要按 task 反查，成本高收益低
    # 2. 采集脚本/无主任务本来就可能没有明确 owner
    #
    # ⚠️ 这一列**只做记录，不参与检索过滤**。这是个刻意的产品选择：
    # P4 的核心卖点是"知识跨任务、跨用户积累"——加过滤会让新账号第一次提问
    # 什么都搜不到。先留好数据，将来真要隔离时不用回填历史。
    # 详见 P4-实施计划.md / M4-实施计划.md 里的决策记录。
    user_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 回填/重建索引时按任务删旧节点、按任务查节点
        Index("knowledge_task", "task_id"),
        # 检索时最常见的过滤维度：先按来源类型缩小范围再算相似度
        Index("knowledge_source", "source_type"),
        # 入库判重：每次写入前查这个哈希是否已存在
        Index("knowledge_hash", "content_hash"),
        # M4 收尾：按采集者查（统计谁贡献了多少 / 将来要隔离时的过滤维度）
        Index("knowledge_owner", "user_id"),
    )


# ---- P5 审批停等 ----


class Approval(Base):
    """一次"高风险动作等人工批准"的记录。

    它在项目里的角色：
    P5a-1 只把待审批信息写进 `runs/<task_id>/meta.json` —— 那是**当前状态**的快照，
    够前端渲染，但回答不了三个必须回答的问题：
    - 谁批的？（多人协作时这是审计的全部意义）
    - 这个任务一共被拦了几次、都被谁放行？（合规场景要能出这份清单）
    - 拒绝理由是什么？（要回喂模型，让它下一步换个做法）
    所以单独一张表：一人一次审批一行，历史可查、可统计。

    为什么不是 task 上的一个 JSON 列：
    ① 一个任务可以有多次审批（第 4 步提交被拦、第 7 步删除又被拦），JSON 要自己
       维护数组和"当前是哪一条"的指针；② `decided_by` 要外键约束到 users；
    ③ "查某个人的审批记录" 用 SQL 一句 where，JSON 得全表扫。

    为什么没有过期时间：
    冻结方案不占任何资源（任务已经退出 worker），等一天也不会有代价。
    过期需要定时扫描 + 额外的状态，v0.1 不做；前端显示"已等待 X 分钟"就够。
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)

    # 一个任务多次审批 → 不是唯一键。CASCADE：任务删了，审批记录跟着走，
    # 否则会留下指向不存在任务的孤儿行（同 knowledge_nodes 的处理）。
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 第几步的决策。⚠️ 只用于展示和排查，**不做唯一键**：
    # workflow 的 _record_rejected() 允许同一个 step_no 出现多条决策（重试不推进步数）。
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # 待审批的动作本体，形如 {"action": "click", "target": "e3", "thought": "...", "value": null}
    # 存 model_dump() 的整份：恢复时要拿它直接执行，少一个字段就得重新问模型
    action: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    # 判定理由（"点击「提交」属于高风险动作（命中关键词：提交）"），直接显示给审批人
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 暂停时所在页面。恢复时先 goto 到这里——浏览器已经关了，页面状态没了
    snapshot_url: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 续跑用的决策上下文：goal / plan / progress / current_url / step_no。
    # 为什么不从 tasks 表读：那几列是任务**结束时**才写终值的，
    # 暂停时 tasks.plan 还是提交时的空列表，续跑会丢掉模型已经做完的分解。
    context: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    # pending / approved / rejected / cancelled
    # cancelled = 任务在等待期间被手动终止或进程异常，这一条不再需要人处理
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")

    # 谁批的。SET NULL 而不是 CASCADE：用户注销了，审批记录应该留下来（审计要留痕），
    # 只是"谁批的"变成不知道。删掉记录等于篡改审计链。
    decided_by: Mapped[int | None] = mapped_column(
        PK, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 审批意见。拒绝理由会回喂 prompt，所以是"有语义的输入"，不是备注
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 最高频查询：这个任务当前有没有待审批项（前端轮询 + 提交决策前的校验）
        Index("approvals_task_status", "task_id", "status"),
    )


class Credential(Base):
    """一份已保存的登录态（P5b）。

    它在系统里的角色：让 Agent **开局就有身份**，从而不需要在任务里填密码。
    这同时解决两件事——要登录的站能进去了，以及"密码明文进审批表"那条泄露路径消失。

    为什么存 storage_state 的密文，而不是账号密码：
    密码是"能推出未来所有会话"的根凭证，而 storage_state 只是"某一个会话的票"。
    票会过期、泄露面小，而且复用它不需要重新登录（不撞验证码/多因素）。
    代价是过期后必须重新保存——前端要把这件事说清楚。

    为什么整块加密成一列（ciphertext）而不是按字段存：
    storage_state 的结构由 Playwright 决定（cookies 数组 + origins 数组，字段可能随版本增减）。
    按字段拆表等于把第三方格式焊进我们的 schema，它一改我们就得迁移。
    整块加密存 Text，结构变化对我们完全透明。
    """

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 用户注销 → 他的登录态跟着删。这里用 CASCADE 而不是 approvals 的 SET NULL：
    # 审批记录是"发生过什么事"的审计证据，必须留；登录态是"能拿来用"的凭证，
    # 主人没了还留着，只是纯粹的风险。
    user_id: Mapped[int] = mapped_column(
        PK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # 站点标识（用 host，例如 127.0.0.1）。一个用户一个站点一份登录态，
    # 见下面的唯一索引——同一个站存两份只会让人纠结"刚才用的是哪一份"
    site: Mapped[str] = mapped_column(Text, nullable=False)
    # 给人看的名字（"张三的测试账号"）。列表里靠它区分，不代表任何权限
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Fernet 密文（见 app/security/vault.py）。**里面绝不能出现明文**
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)

    # 最后一次被任务用到的时刻。用来回答"这份还能用吗/多久没用了"
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 会话的过期时间（取自 storage_state 里最晚的那个 cookie expires）。
    # 为什么要存：cookie 过期了任务会莫名其妙地被弹回登录页，
    # 有这个值就能在提交任务前提示"这登录态已过期"，而不是让人靠猜。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 一个用户在一个站点只保留一份。重复保存视为"更新"（见 repo.save_credential）
        Index("credentials_user_site", "user_id", "site", unique=True),
    )
