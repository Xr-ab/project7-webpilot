"""登录态接口（P5b）：保存 / 列出 / 删除。

它在项目里的角色：让"Agent 有身份"变成一件用户能操作的事。
保存时后端**代用户登录一次**并立刻导出会话（storage_state），密码用完即弃；
之后任务提交带 `credential_id`，worker 解密后注入浏览器，开局就是登录态。

🔴 这个文件里最重要的三件事，都跟密码有关：
1. **密码只在内存里活一次**：进来 → 交给 browser/login.py 填进页面 → 函数结束即丢。
   不写日志、不写库、不进返回值、不进任何给模型看的字符串。
2. `SaveCredentialRequest` 的两个敏感字段带 `repr=False`：这样任何
   `log.debug(req)` / 异常信息里的 repr 都不会带出账号密码（Pydantic v2 支持）。
3. 返回体只有 id / 站点 / 名字 / 过期时间 / cookie 条数，**永远不回密文**。

为什么保存要同步等（不像提交任务那样丢后台）：
它是个 2~5 秒的一次性开通动作，用户点了就该看到"成功/账号密码不对"。
丢后台反而要多一套"这个保存任务跑到哪了"的状态机，为用户一年点一次的操作不值。
"""
from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.tasks import assert_allowed_url
from app.auth.deps import get_current_user
from app.browser.login import LoginError, capture_login_state, expires_at_from_state
from app.db import repo
from app.db.models import User
from app.security import vault

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/credentials", tags=["credentials"])


class SaveCredentialRequest(BaseModel):
    """保存一份登录态所需的全部输入。

    `username` / `password` 带 `repr=False`：Pydantic 的 repr 会带全部字段，
    而这个对象一旦被日志、traceback 或调试器打印出来，密码就跟着出去了。
    这是"防手滑"而不是"防攻击"——但泄露事故基本都是手滑造成的。
    """

    login_url: str = Field(min_length=1, max_length=500, description="登录页地址（须在白名单内）")
    username: str = Field(min_length=1, max_length=200, repr=False, description="站点账号")
    password: str = Field(min_length=1, max_length=200, repr=False, description="站点密码（用完即弃）")
    # 列表里显示的名字。留空时用账号名——账号不是密钥（它就在页面 HTML 里），
    # 但**密码永远不能当标签**
    label: str = Field(default="", max_length=100, description="显示名，留空用账号名")


def _site_of(login_url: str) -> str:
    """登录页所属的站点标识（host，不含端口）。

    为什么用 host 而不是 netloc（host:port）：cookie 的域是"主机"级别的，
    同一个 host 换端口仍是同一份会话。用 netloc 会把 8000 和 5173 当成两个站点，
    于是 Vite 代理访问时存的那份在直连 8000 时用不上——那种"明明存了却说没有"的
    问题排查起来很浪费时间。
    """
    return (urlparse(login_url).hostname or "").lower()


@router.post("", summary="登录一次并把会话加密存起来")
async def save_credential(
    req: SaveCredentialRequest,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """用给的账号密码登录一次 → 导出 storage_state → 加密落库。

    为什么必须要求登录（不能像任务那样在认证关闭时挂占位用户）：
    登录态是**凭证**，必须有个主人才能做归属隔离。认证关闭模式下没有真实用户，
    存下来就变成"谁都能用"，那比不存更糟。
    """
    assert_allowed_url(req.login_url)
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="保存登录态需要登录账号（当前是认证关闭模式，登录态没有归属人）",
        )

    site = _site_of(req.login_url)
    if not site:
        raise HTTPException(status_code=400, detail="login_url 里解析不出站点")

    try:
        result = await capture_login_state(
            login_url=req.login_url.strip(),
            username=req.username,
            password=req.password,
        )
    except LoginError as exc:
        # 登录没成功：如实告诉用户（账号密码不对 / 找不到按钮 / 不是登录页）。
        # 这些都是"用户能自己改"的问题，所以是 400 而不是 500。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # 真正的意外（浏览器起不来等）。日志里**只记类型和站点**，不记请求体
        log.warning("保存登录态时异常：site=%s err=%s", site, type(exc).__name__)
        raise HTTPException(status_code=500, detail="保存登录态时出错，请稍后重试") from exc

    state = result["storage_state"]
    expires_at = expires_at_from_state(state)
    label = req.label.strip() or req.username

    credential_id = await repo.save_credential(
        user_id=user.id,
        site=site,
        label=label,
        ciphertext=vault.encrypt(state),
        expires_at=expires_at,
    )
    if credential_id is None:
        # 登录成功了但写不进去：这不是"登录失败"，要说清是存储问题，
        # 否则用户会反复检查自己的密码
        raise HTTPException(
            status_code=503, detail="登录成功，但保存失败（数据库不可用），请稍后重试"
        )

    return {
        "id": credential_id,
        "site": site,
        "label": label,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": False,
        "cookie_count": result["cookie_count"],
        # 前端据此提示"会话型 cookie：关掉浏览器就失效，可能需要经常重存"
        "note": "" if expires_at else "这份是会话型 cookie（关浏览器即失效）",
    }


@router.get("", summary="列出我保存的登录态（不含密文）")
async def list_credentials(
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """只列自己的。降级口径和 /stats、/tasks 一致：DB 不可用 → 200 + available=false。"""
    if user is None:
        return {"available": False, "note": "认证关闭模式下没有登录态", "items": []}
    if not await repo.ping():
        return {"available": False, "note": "数据库不可用，读不到登录态", "items": []}
    return {"available": True, "items": await repo.fetch_credentials(user.id)}


@router.delete("/{credential_id}", summary="删掉一份登录态")
async def delete_credential(
    credential_id: int,
    user: Annotated[User | None, Depends(get_current_user)],
) -> dict:
    """删自己的。别人的返回 404（不返回 403）：

    403 等于承认"这个 id 存在，只是不是你的"，而 id 是自增的——
    攻击者能靠它数出全站存了多少份登录态。404 什么都不泄露。
    """
    if user is None:
        raise HTTPException(status_code=400, detail="认证关闭模式下没有登录态可删")
    if not await repo.delete_credential(credential_id, user.id):
        raise HTTPException(status_code=404, detail="没有这份登录态")
    return {"deleted": credential_id}
