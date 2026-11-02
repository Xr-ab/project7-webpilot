"""一次性登录，拿到 storage_state（P5b）。

它在系统里的角色：把"人有一次账号密码"变成"系统有一张可复用的会话票"。
这是个**一次性开通动作**，不属于任务执行——所以它放在 browser/ 下、不进 workflow。

🔴 最硬的一条约束：**这里绝不能让模型参与。**
让 Agent"自己去填密码"意味着密码会进 prompt、发给第三方 LLM API。
所以这一段是写死的 Playwright 操作：找密码框 → 找它前面最近的文本输入框当账号框 →
填 → 点提交。代价是只支持结构常规的登录页（有 `input[type=password]` 且账号框在它前面），
这个边界写在文档里，不假装通用。

📋 密码的三条纪律（任何一条破了都等于泄露）：
1. 不进日志：本模块的日志只写"哪个站点、cookie 条数"，从不写账号密码
2. 不进返回值：返回的只有 storage_state（票）和少量元信息
3. 不进 prompt/trace：这一层不经过 workflow，天然不会进 trace；也不许被别的模块
   拼进任何给模型看的字符串

成功判据：提交后**密码框不再可见**。这个判据对常规登录页成立（登录成功就跳走了），
对"登录失败但页面还在"也成立（登录页的密码框还在）——两种都能分辨，所以够用。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from playwright.async_api import Locator, Page

from app.browser.session import BrowserSession

log = logging.getLogger(__name__)

# 提交之后给页面多久跳到下一页。800ms 足够本地站，真实站点主要靠
# wait_for_load_state 兜（它在导航发生时才会返回）
_POST_SUBMIT_WAIT_MS = 800

# 提交按钮的兜底文字匹配。放在最后试：优先用 [type=submit]，
# 因为文字匹配会误命中"登录帮助""忘记密码"这类链接式按钮
_SUBMIT_TEXT = re.compile(r"登录|登陆|登入|sign\s*in|log\s*in", re.IGNORECASE)


class LoginError(RuntimeError):
    """登录没成功。message 会原样进 API 的 400 detail，所以话要写给人看。"""


def _mark_username_field_js() -> str:
    """在页面上给"密码框前面最近的文本输入框"打一个临时标记。

    为什么用"打标记 + 按标记选"而不是直接把元素 handle 返回：
    Playwright 的 fill 需要 Locator。用标记属性选出来的 Locator 和 snapshot.py
    给元素打 data-wp-id 是同一个套路——**位置关系由 JS 算一次，后续操作走标准 API**。
    比在 Python 里算 nth-child 路径稳定得多。

    只认 text/email/tel/无 type 四种：账号框基本就这几种，
    把 password/hidden/checkbox 也算进来只会选错元素。
    """
    return """
    () => {
      const pw = document.querySelector('input[type="password"]');
      if (!pw) return false;
      const all = Array.from(document.querySelectorAll('input'));
      const idx = all.indexOf(pw);
      for (let i = idx - 1; i >= 0; i -= 1) {
        const type = (all[i].getAttribute('type') || 'text').toLowerCase();
        if (type === 'text' || type === 'email' || type === 'tel' || type === '') {
          all[i].setAttribute('data-wp-login-user', '1');
          return true;
        }
      }
      return false;
    }
    """


async def _find_submit(page: Page) -> Locator | None:
    """找登录按钮。三级兜底：submit 类型 → input[type=submit] → 文字匹配。"""
    for selector in ('button[type="submit"]', 'input[type="submit"]'):
        locator = page.locator(selector)
        if await locator.count() > 0:
            return locator.first
    locator = page.get_by_role("button", name=_SUBMIT_TEXT).first
    if await locator.count() > 0:
        return locator
    return None


async def _password_visible(page: Page) -> bool:
    """密码框是否还看得见。

    这是"登录成没成"的唯一判据（理由见文件头）。任何异常都当"看不见"，
    因为抛异常时页面通常已经跳走了——这时候更需要继续导出 state 而不是报错。
    """
    try:
        locator = page.locator('input[type="password"]').first
        if await locator.count() == 0:
            return False
        return await locator.is_visible()
    except Exception:
        return False


def expires_at_from_state(state: dict) -> datetime | None:
    """从 storage_state 里算"最晚什么时候过期"。

    为什么取最大值：只要还有**一个** cookie 没过期，会话通常就还活着
    （真实站点的会话 cookie 才是关键那个）。取最小值会把"长期 cookie + 一个立刻过期的
    统计 cookie"误判成"马上失效"，让用户白白重新保存。

    expires <= 0 的是**会话 cookie**（关浏览器即失效，Playwright 用 -1 表示）——
    这类不能参与取最大值，否则会把 expires_at 算成 1970 年。
    全是会话 cookie 时返回 None：前端据此显示"会话型（关浏览器失效）"，
    比编一个时间诚实。
    """
    best = 0.0
    for cookie in state.get("cookies") or []:
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            best = max(best, float(expires))
    if best <= 0:
        return None
    return datetime.fromtimestamp(best, tz=timezone.utc)


async def capture_login_state(
    *,
    login_url: str,
    username: str,
    password: str,
    timeout_ms: int = 20_000,
) -> dict:
    """在 login_url 上登录一次，返回 {storage_state, url, cookie_count}。

    调用方负责：立刻加密 storage_state、之后立刻丢弃明文密码。
    """
    if not username or not password:
        raise LoginError("账号和密码都要填")

    # 用 BrowserSession 而不是裸 playwright：白名单闸门（_assert_host）直接复用，
    # 登录页地址也就自动受"只能访问 ALLOWED_HOSTS"约束。
    async with BrowserSession(headless=True) as session:
        page = session.page
        assert page is not None

        try:
            await session.goto(login_url)
        except PermissionError as exc:
            # 白名单拦下：这不是"登录失败"，是配置问题，单独说清楚
            raise LoginError(str(exc)) from exc
        except Exception as exc:
            raise LoginError(f"打不开登录页：{type(exc).__name__}: {exc}") from exc

        if await page.locator('input[type="password"]').count() == 0:
            raise LoginError("这个页面上没有密码输入框，不像是登录页（或页面结构不常规）")

        marked = await page.evaluate(_mark_username_field_js())
        if not marked:
            raise LoginError("在密码框前面找不到账号输入框")

        try:
            await page.locator('[data-wp-login-user="1"]').fill(username)
            await page.locator('input[type="password"]').first.fill(password)
        except Exception as exc:
            raise LoginError(f"填账号密码时出错：{type(exc).__name__}") from exc

        submit = await _find_submit(page)
        if submit is None:
            raise LoginError("找不到登录按钮（没有 submit，也没有文字像登录的按钮）")

        try:
            await submit.click()
        except Exception as exc:
            raise LoginError(f"点登录按钮失败：{type(exc).__name__}") from exc

        # 等跳转。wait_for_load_state 在"导航发生"时返回；没跳转就落到超时，
        # 由下面的密码框判据兜——所以这里吞掉异常是对的，不是偷懒。
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        await page.wait_for_timeout(_POST_SUBMIT_WAIT_MS)

        if await _password_visible(page):
            # 只记站点，不记任何输入内容
            log.info("登录未成功（密码框仍在）：%s", login_url)
            raise LoginError("登录后仍停在登录页，账号或密码可能不对")

        state = await session.export_storage_state()
        cookie_count = len(state.get("cookies") or [])
        if cookie_count == 0:
            # 没有 cookie 就没法复用会话，存下来也是个空壳——宁可在这一步就说清楚
            raise LoginError("登录后没有拿到任何 cookie，这个站点可能不支持会话复用")

        log.info("登录成功并导出会话：%s（%d 条 cookie）", login_url, cookie_count)
        return {"storage_state": state, "url": page.url, "cookie_count": cookie_count}
