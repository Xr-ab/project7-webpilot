"""批处理脚本的共用小工具。

为什么单独立这个文件（P5b-④）：P5 上线任务级 require_approval 参数后，
每个批处理脚本都要记得写 `require_approval: False`——忘了写的后果不是报错，
而是任务静默卡在 waiting_approval 等一个永远不来的人（第十节的回归防护
漏了 4 个脚本，靠逐个实跑才发现）。教训是：**"想到的"清点必然漏，
要靠一处实现去兜住**。这个 payload 构造函数就是那个"一处"：
默认 False 写死在这里，新脚本用它可以少记一条纪律、漏了也不会踩坑。

只管构造、不管发送：脚本分同步（httpx.Client / urllib）和异步
（httpx.AsyncClient）两派，POST 的姿势各不相同，硬统一会把简单的
事情搞复杂。需要显式开审批的脚本（check_approval / check_redact）
自己传 require_approval=True，语义反而更醒目。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# ---- 评测账号（M4 之后 /api/v1/tasks 要 token，批处理脚本得先自证身份）----
# 固定账号而不是每次随机注册：评测要能跨轮对比同一账号下的数据
EVAL_EMAIL = "eval@webpilot.dev"
EVAL_PASSWORD = "eval-real-2026"


def auth_headers(token: str) -> dict:
    """带 token 的请求头。token 为空（认证关闭的机器）返回空 dict——
    同一份脚本代码在"开认证"和"关认证"两种部署下都能跑。
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


def ensure_token(
    base_url: str, email: str = EVAL_EMAIL, password: str = EVAL_PASSWORD
) -> str:
    """拿评测账号的 token：先注册（已存在 409 → 改登录）；认证关闭（403）返回空串。

    为什么放 common：M4 开认证后，没带 token 的批处理脚本全部 401——
    这比"漏 require_approval"更隐蔽，因为脚本是在 health check 通过后
    才在第一个任务提交上炸的。收进一处，新脚本 import 一下就不会再漏。
    """

    def _post_auth(path: str) -> str:
        data = json.dumps({"email": email, "password": password}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["token"]

    try:
        return _post_auth("/api/v1/auth/register")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return _post_auth("/api/v1/auth/login")
        if exc.code == 403:
            return ""
        raise


def task_payload(
    goal: str,
    start_url: str,
    *,
    credential_id: int | None = None,
    require_approval: bool = False,
) -> dict:
    """构造 POST /api/v1/tasks 的请求体。

    require_approval 默认 False：批处理无人值守，开了审批会卡在等人
    （P5a 教训：那次超时会被记成"任务失败"，直接把成功率算低）。
    要测审批闸门的脚本必须显式传 True——让"这个脚本要审批"写在调用处一眼可见。

    credential_id 传了才进 payload：不传和传 null 在 API 层等价，
    但省略让"这个任务没带登录态"在抓包/日志里更直观。
    """
    payload: dict = {
        "goal": goal,
        "start_url": start_url,
        "require_approval": require_approval,
    }
    if credential_id is not None:
        payload["credential_id"] = credential_id
    return payload
