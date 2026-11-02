"""任务状态的取值约定与分组（唯一定义处）。

为什么需要这个文件（P5 加状态时踩出来的）：
status 这个字符串跨五个层在用——workflow 写、repo 查、API 返回、前端渲染、评测统计。
加 `waiting_approval` 之前，`("queued", "running")` 这个元组被硬编码在 repo.py 的三处，
含义还各不相同（一处判"终态"、一处判"进行中"、一处判"该不该被启动补偿改掉"）。
加新状态时只改了看得见的那两处，第三处悄悄错——这正是本次把它收敛成一个模块的原因。

三个分组（**不要再用裸字符串元组**）：

    NEEDS_WORKER  需要 worker 推进、占并发槽 → queued / running
    PAUSED        任务还活着但没人在推，等人  → waiting_approval
    UNFINISHED    = NEEDS_WORKER + PAUSED     → "还没出结果"

统计口径的关键区别：**待审批不属于终态**。如果把它算进终态，
成功率分母会混进一批"还没跑完、只是卡着等人"的任务，数字直接失真。
但它也不属于 NEEDS_WORKER——它不占 worker，`mark_interrupted` 不该把它当中断改掉
（否则用户去泡了杯茶回来，任务被标成"进程重启，已中断"）。
"""
from __future__ import annotations

# 需要 worker 推进的状态（占并发槽、会被启动补偿清理）
NEEDS_WORKER: tuple[str, ...] = ("queued", "running")

# 暂停但任务仍然有效（等人、不占资源、不该被启动补偿动）
PAUSED: tuple[str, ...] = ("waiting_approval",)

# 还没有最终结论的状态 = 两类之和。
# 统计"成功率"时要把它们从分母里排除；"还有几个没跑完"也要按它计数。
UNFINISHED: tuple[str, ...] = NEEDS_WORKER + PAUSED

# 给前端/日志看的统一中文名。前端的 STATUS_TEXT 是另一份（TS），
# 两边都要有是因为前端不该为了一个文案去请求接口。
STATUS_LABELS: dict[str, str] = {
    "queued": "排队中",
    "running": "执行中",
    "waiting_approval": "待审批",
    "success": "已完成",
    "error": "出错",
    "timeout": "超时",
    "loop": "循环",
    "step_limit": "步数用尽",
    "invalid_action": "动作不合法",
    "nav_failed": "导航失败",
    "bad_url": "地址不合法",
    "bad_request": "参数不合法",
    "interrupted": "已中断",
}


# ---- 审批项（approvals.status）的取值 ----
# 和任务 status 是**两套**状态机，别混用：
# 任务说"这个任务现在在等人"，审批项说"这一条请求被怎么处理了"。
# 一个任务可以有多条审批项：第 4 步这条 approved，第 7 步又出一条 pending。
APPROVAL_PENDING = "pending"      # 等人处理（唯一会出现在前端卡片上的状态）
APPROVAL_APPROVED = "approved"    # 批了，动作会执行
APPROVAL_REJECTED = "rejected"    # 拒了，理由回喂模型重新决策
APPROVAL_CANCELLED = "cancelled"  # 没必要再处理了（任务被终止 / 进程死在等待期间）

# 已经有人处理过、不能再批第二次的集合。
# 为什么用"最终态集合"而不是直接判 != pending：报错信息要能区分
# "已被批过"（409 重复操作）和"已经取消了"（换个提示，用户该重跑任务）
APPROVAL_RESOLVED: tuple[str, ...] = (
    APPROVAL_APPROVED,
    APPROVAL_REJECTED,
    APPROVAL_CANCELLED,
)
