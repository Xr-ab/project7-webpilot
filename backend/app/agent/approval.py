"""审批闸门：判断"这一步动作该不该先问人"（P5a）。

它在系统里的位置：`decide` 之后、`act` 之前。模型已经想好要做什么，
但还没真做——这是唯一能插入人工判断、且**不需要回滚任何副作用**的时刻。
放在 act 之后就只能"事后报警"了。

为什么规则用关键词而不是"让模型自己判断风险"：
- 让模型判断 = 把安全边界交给被测对象，注入一句"忽略之前的规则"就可能绕过
- 关键词清单是**确定性**的：同一步动作，问一百次结果一样，可写测试、可审计
- 缺点很明确：文案变体会漏。所以清单集中一处、随时可扩，并接受"漏判"是已知边界

为什么密码框要靠 `input_type` 而不是字段文案：
密码框的 value 在快照里被打码成 `***`，从文本上跟普通输入框没有区别。
靠"字段名叫密码"猜，遇到英文站（Password）、或字段名写"登录凭证"就漏。
`input_type` 是 DOM 上的客观属性，不依赖文案。
"""
from __future__ import annotations

from app.agent.actions import Action, ActionType
from app.browser.snapshot import PageSnapshot

# 点了会"产生后果"的词。清单故意保守：
# 宁可少拦（漏判是已知边界），也不要把"确认收货地址"这种正常步骤也拦下来——
# 审批次数一多，用户会养成"闭眼点批准"的习惯，闸门就废了。
RISKY_CLICK_KEYWORDS: tuple[str, ...] = (
    "提交",
    "删除",
    "支付",
    "购买",
    "下单",
    "确认",
    "结账",
    "结算",
    "退出",
    "注销",
    "发送",
    "转账",
)

# 需要审批的输入框类型。password：填密码意味着正在凭证化操作（登录/改密）。
RISKY_INPUT_TYPES: tuple[str, ...] = ("password",)


def _find_element(snapshot: PageSnapshot | None, target: str | None) -> dict | None:
    """按本轮元素编号（e7）找回快照里的元素。找不到返回 None。

    找不到是正常情况：快照有元素上限，模型可能引用了一个已被挤掉的编号——
    那种情况下动作层自己会报"编号不存在"，闸门没必要抢着报错。
    """
    if not snapshot or not target:
        return None
    for el in snapshot.get("elements") or []:
        if el.get("id") == target:
            return el
    return None


def is_password_input(snapshot: PageSnapshot | None, target: str | None) -> bool:
    """target 指向的元素是不是密码框（或其它 RISKY_INPUT_TYPES 里的敏感框）。

    为什么抽成公共函数而不是 gate 私有：trace 脱敏（workflow._current_secret）
    也要回答同一个问题——"这个动作的 text 是不是密码"。判定标准必须只有一份：
    两处各写一遍，以后 RISKY_INPUT_TYPES 加了新类型（比如 otp），
    闸门拦了但脱敏没跟上，密码就静默地流进落盘轨迹里了。
    """
    element = _find_element(snapshot, target)
    input_type = ((element or {}).get("input_type") or "").lower()
    return input_type in RISKY_INPUT_TYPES


def gate(action: Action, snapshot: PageSnapshot | None) -> str | None:
    """返回 None = 放行；返回字符串 = 停下来问人的理由（这段文字会显示给用户）。

    理由要写成"人能一眼判断该不该批"的句子，不能只说"命中规则 3"——
    用户看不到规则表，他只能看懂"Agent 要去点【提交订单】"。
    """
    element = _find_element(snapshot, action.target)
    label = (element or {}).get("text") or action.target or "未知元素"

    if action.action is ActionType.CLICK:
        text = ((element or {}).get("text") or "").lower()
        if not text:
            # 没有文字的按钮（纯图标）无法判定，放行。
            # 记一笔在理由里没有意义——用户看到"未知元素"也没法决策。
            return None
        for word in RISKY_CLICK_KEYWORDS:
            if word in text:
                return f"点击「{label}」属于高风险动作（命中关键词：{word}）"
        return None

    if action.action is ActionType.TYPE:
        # 判定走公共函数：和 trace 脱敏共用同一份"什么是密码框"的标准
        if is_password_input(snapshot, action.target):
            return f"在密码框「{label}」中输入内容属于高风险动作"
        return None

    # 其余动作（goto / select / scroll / finish）不拦：
    # - goto 有域名白名单，越界在动作层就被拒
    # - scroll / select 不产生副作用（选择项也可能有后果，但测试站里没有，先不拦）
    # - finish 是给答案，不是操作页面
    return None
