"""动作层：模型想干的事，先翻译成"白名单内的合法动作"，再交给浏览器去执行。

为什么单独一个文件：
- prompt 里写给模型看的是一回事，程序里能执行的是另一回事。中间必须有道闸。
- 模型输出是自由文本，它可能编造编号、访问外网、传空字符串、甚至塞一段注入字符串。
- 所有校验集中在这里，改规则只动一处；单测也只测这一处。

三层校验（缺一层都会出事故）：
1. 形状：字段齐不齐、类型对不对（Pydantic 负责）
2. 语义：编号在不在本轮快照里、目标元素类不类型对（本文件负责）
3. 安全：goto 的域名在不在白名单（本文件负责，session 里还有第二道）

校验失败一律抛 ActionError，**不抛裸异常**。上层拿到它的 message 回喂给模型重试一次——
模型经常只是漏了个字段或者把编号写成 e0，直接崩掉太浪费。
"""
from __future__ import annotations

from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

# 输入文本长度上限。防模型把整段正文塞进输入框（撑爆页面 + 白烧 token）
MAX_TEXT_LEN = 200


class ActionType(str, Enum):
    """v0.1 只开放 6 个动作。

    刻意不加上传文件、切标签页、处理弹窗：T1-T5 都用不到，
    多一个动作就多一套校验和一片出事的地方。等有任务真的需要再加。
    """

    GOTO = "goto"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    FINISH = "finish"


class ActionError(ValueError):
    """动作不合法。message 会原样回喂给模型，所以话要写得能让它照着改。"""


class RawAction(BaseModel):
    """模型输出的原始形状：字段尽量宽松，宽松的地方由 check_action 兜。"""

    thought: str = ""
    action: str = ""
    target: str | None = None
    text: str | None = None
    option: str | None = None
    url: str | None = None
    direction: str | None = None
    answer: str | None = None


class Action(BaseModel):
    """校验通过的动作：字段已归一化，可以直接交给 session 执行。"""

    thought: str
    action: ActionType
    target: str | None = None
    text: str | None = None
    option: str | None = None
    url: str | None = None
    direction: str = "down"
    answer: str | None = None


def parse_raw(payload: dict[str, Any]) -> RawAction:
    """把模型给的 dict 转成 RawAction。

    Pydantic 报错信息是英文且面向开发者，这里翻译成模型看得懂的一句话。

    ⚠️ 为什么要把"正确形状"直接写进报错里（P7-D1 T6 实测）：
    原来的文案只有"动作格式不对：Input should be a valid string"。模型把 action
    写成了一个对象（`"action":{"action":"type","target":"e1",...}`），看到这句
    英文提示后**完全不知道该改成什么样**，于是原样又发了一遍，两次就撞上
    invalid_action 收场。弱模型不会从"类型错了"反推出正确 JSON，必须把样例摆在它眼前。
    """
    try:
        return RawAction(**payload)
    except ValidationError as exc:
        bad = exc.errors()[0]
        loc = ".".join(str(x) for x in (bad.get("loc") or ())) or "字段"
        raise ActionError(
            f"动作格式不对：{loc} 的类型错了（{bad.get('msg', '')}）。"
            '正确形状例如 {"thought":"一句话","action":"type","target":"e1","text":"李四"}：'
            "action 只填字符串（goto/click/type/select/scroll/finish），"
            "target/text/option 才是它的参数，不要把 action 写成对象。"
        ) from exc


def _need_target(name: str, raw: RawAction, elements: dict[str, dict]) -> str:
    """所有带 target 的动作共用的检查：编号必须存在、且必须是本轮快照里的。"""
    target = (raw.target or "").strip()
    if not target:
        raise ActionError(f"{name} 缺少 target：必须填快照里的元素编号，例如 e3")
    if target not in elements:
        available = "、".join(list(elements)[:12]) or "（本轮没有可交互元素）"
        raise ActionError(
            f"编号 {target} 不在本轮快照里（不要自己编）；本轮可用编号：{available}"
        )
    return target


def _need_role(target: str, elements: dict[str, dict], allowed: set[str], hint: str) -> None:
    """元素类型要对得上：不能在链接上打字，也不能在下拉框上点。"""
    role = elements[target].get("role", "")
    if role not in allowed:
        raise ActionError(f"{target} 是 {role or '未知类型'}，{hint}")


def check_action(
    raw: RawAction,
    *,
    elements: dict[str, dict],
    allowed_hosts: tuple[str, ...],
) -> Action:
    """把 RawAction 校验成 Action。所有规则都在这里，别处不许再判一遍。"""
    name = (raw.action or "").strip().lower()
    if name not in {t.value for t in ActionType}:
        available = "、".join(t.value for t in ActionType)
        raise ActionError(f"未知动作 {raw.action!r}；可用动作：{available}")

    thought = (raw.thought or "").strip()[:300]

    if name == ActionType.GOTO.value:
        url = (raw.url or "").strip()
        if not url:
            raise ActionError("goto 缺少 url")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ActionError(f"goto 的 url 必须以 http:// 或 https:// 开头，收到 {url!r}")
        host = (parsed.hostname or "").lower()
        if host not in allowed_hosts:
            raise ActionError(
                f"域名 {host} 不在白名单（允许：{'、'.join(allowed_hosts)}）；不允许访问外部站点"
            )
        return Action(thought=thought, action=ActionType.GOTO, url=url)

    if name == ActionType.CLICK.value:
        target = _need_target("click", raw, elements)
        return Action(thought=thought, action=ActionType.CLICK, target=target)

    if name == ActionType.TYPE.value:
        target = _need_target("type", raw, elements)
        # 允许的角色要包含 ARIA 的"文本框"一族，不能只认原生 input/textarea：
        # 真实站点（MDN 搜索框）写的是 <input type="search" role="combobox">，
        # 我们的 roleOf 优先读显式 role，于是它是 combobox。
        # 只放行 input/textarea 的话，明明是可输入的框也会被判"不能输入"，
        # 模型只能去点别的 element，任务直接废。
        # 这条校验的本意是"别往按钮/链接里打字"，combobox/searchbox/textbox 不违背这个本意。
        _need_role(
            target,
            elements,
            {"input", "textarea", "combobox", "searchbox", "textbox"},
            "不能往它里面输入文字（需要 input / textarea / combobox 这类可输入元素）",
        )
        text = raw.text if raw.text is not None else ""
        if not text.strip():
            raise ActionError("type 的 text 不能为空")
        if len(text) > MAX_TEXT_LEN:
            raise ActionError(f"text 过长（{len(text)} 字符），上限 {MAX_TEXT_LEN}")
        return Action(thought=thought, action=ActionType.TYPE, target=target, text=text)

    if name == ActionType.SELECT.value:
        target = _need_target("select", raw, elements)
        _need_role(target, elements, {"select"}, "不是下拉框，选不了（需要 select）")
        option = (raw.option or "").strip()
        if not option:
            raise ActionError("select 缺少 option：要填下拉项里显示的文字，例如 福州")
        return Action(thought=thought, action=ActionType.SELECT, target=target, option=option)

    if name == ActionType.SCROLL.value:
        direction = (raw.direction or "down").strip().lower()
        if direction not in ("up", "down"):
            raise ActionError(f"scroll 的 direction 只能是 up 或 down，收到 {direction!r}")
        return Action(thought=thought, action=ActionType.SCROLL, direction=direction)

    # 只剩 finish
    answer = (raw.answer or "").strip()
    if not answer:
        raise ActionError("finish 缺少 answer：任务完成时要说明结果，不能空手结束")
    return Action(thought=thought, action=ActionType.FINISH, answer=answer)


def describe(action: Action) -> str:
    """一行人类可读的动作描述，写进 trace / 打日志用。"""
    if action.action is ActionType.GOTO:
        return f"goto {action.url}"
    if action.action is ActionType.CLICK:
        return f"click {action.target}"
    if action.action is ActionType.TYPE:
        return f"type {action.target} <- {action.text!r}"
    if action.action is ActionType.SELECT:
        return f"select {action.target} <- {action.option}"
    if action.action is ActionType.SCROLL:
        return f"scroll {action.direction}"
    return f"finish {action.answer!r}"
