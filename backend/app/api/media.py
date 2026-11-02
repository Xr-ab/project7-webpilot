"""任务媒体的受控访问（M4 收尾）：取代原来裸挂的 StaticFiles。

历史问题（一句话）：`app.mount("/runs", StaticFiles(...))` 意味着
`/runs/<task_id>/meta.json` 不需要任何凭证就能读，把 M4 的归属隔离整个绕过去了。

这个路由做三件事，顺序不能换：
    1. 只放行**媒体后缀**（png/jpg/webp/mp3...）—— meta.json / trace.json
       即使签名正确也一律不给。理由：这两个文件是"任务全文"，
       正常路径是一律走 `GET /api/v1/tasks/{id}`（带归属校验）；
       留在静态目录里没有任何场景需要，属于纯粹的攻击面。
    2. 防路径穿越 —— 拒绝 `..`、绝对路径、子目录。
       （FastAPI 的路径参数本身不跨 `/`，但 `%2e%2e%2f` 这类编码经解码后可能带斜杠，
        所以仍要显式校验一遍。）
    3. 验 HMAC 签名 + 过期时间 —— 见 app/storage/signing.py。

为什么不用 StaticFiles 加中间件校验：
    StaticFiles 是 ASGI 应用，中间件层面看不到"这个请求最终要读哪个文件"，
    想校验签名就得自己解析 path。既然都要解析，不如直接写路由，逻辑一目了然，
    还能顺手加上后缀白名单这种 StaticFiles 表达不了的东西。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.storage import signing
from app.storage.files import RUNS_ROOT

router = APIRouter(tags=["media"])

# 只服务这些后缀。故意不含 .json：任务元数据必须走带归属校验的接口。
_ALLOWED_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".m4a", ".wav", ".webm", ".ogg"}
)


def _assert_safe_name(value: str, label: str) -> None:
    """挡路径穿越（`..`、`a/../../b`、绝对路径）。

    为什么 `Path(value).name != value` 就够了：
    Path("a/b").name == "b" ≠ "a/b" → 拦下；Path("..").name == ".." 
    看起来相等，所以额外判一次 ".."。两层都留着，造价几乎为零。
    """
    if not value or value in (".", "..") or Path(value).name != value:
        raise HTTPException(status_code=400, detail=f"{label} 不合法")


@router.get("/runs/{task_id}/{filename}", summary="读取任务媒体（截图/语音，需签名）")
async def get_media(
    task_id: str,
    filename: str,
    e: str | None = Query(default=None, description="过期时间戳（由后端签名时写入）"),
    sig: str | None = Query(default=None, description="HMAC 签名（由后端签名时写入）"),
) -> FileResponse:
    """返回截图或语音文件。

    403 而不是 401：这里没有"登录"这回事（图片请求带不了 token），
    凭证就在 URL 里，缺凭证 = 无权访问本地资源。
    提示语统一说"地址无效或已过期"，不区分是签名错还是过期 ——
    区分开来只会帮攻击者判断自己猜对了哪一半。
    """
    # 第 1 步：后缀白名单
    if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=403,
            detail="该类型的任务文件不通过此路径提供，请使用 /api/v1/tasks/{task_id}",
        )

    # 第 2 步：路径穿越
    _assert_safe_name(task_id, "task_id")
    _assert_safe_name(filename, "filename")

    # 第 3 步：验签
    if not signing.verify(task_id, filename, e, sig):
        raise HTTPException(status_code=403, detail="地址无效或已过期，请刷新页面重新获取")

    target = RUNS_ROOT / task_id / filename
    # 双保险：resolve 后再确认真的落在 runs 目录下（防符号链接绕过）
    try:
        target.resolve().relative_to(RUNS_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="路径不合法") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(target)
