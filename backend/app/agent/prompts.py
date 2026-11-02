"""提示词：全项目唯一的"跟模型说话"的地方。

为什么集中在一个文件：
- 提示词改一个字就可能让成功率掉一大截，必须能一处改、可比对、可回滚
- 散落在各处的提示词没法做 A/B，出问题也说不清是哪句导致的

⚠️ 这份提示词是实测调出来的，不是想当然写的（GLM-4-Flash 的实测结论）：
v1 写法是"先列一张详细动作表 + 8 条规则"，结果模型每次都从动作表里挑一个动作
（在列表页把 6 个商品名全摆在正文里，它还去点"筛选"、去 scroll），连续 3 轮都不 finish。
v2 改成「先判断信息够不够，够了就 finish」放最前面，动作表压到一行一个、规则只留硬规则，
同一份快照下立刻变成正确的 finish。
根因：动作表放在最前面时，模型会把它当成"菜单"，倾向于挑一个动作来"做点什么"；
而任务是读取类任务时，正确的动作恰恰是**什么都不做、直接给答案**。

⚠️ few-shot 示例的选题纪律（P7-D1 T9 实测踩出来的）：
示例**不能和常见任务形态撞场景**。原来的示例 4 讲"信息够了就收尾"，场景写成
"刚点完下一页、读新页第一条的作者"，答案给了 "Marilyn Monroe"。结果遇到
"点下一页后判断还能不能继续翻"的任务时，模型在"我刚点了下一页"这个状态下
直接把示例的 thought 和 answer 原样吐了出来（答案是 "Marilyn Monroe"，而页面
跟这句话毫无关系）——抄示例比看页面省力，弱模型必然抄。
所以示例的场景要么抽象掉、要么换成评测题里不存在的形态；答案也别用能当"万能答案"的短句。

⚠️ 弱模型不做否定判断（P7-D1 T9 实测）：目标元素不存在时，它不会回答"没有"，而是
**离开这一页去别处找**——因为它把 plan 里"点下一页"当成了必须打勾的任务。表现是
详情页之间来回翻 5 次、最后在错的那一页作答。
试过的错误修法：加一条规则"判断类问题'没有'也是答案，别去别的页面找"。结果 T9 是过了，
但 T5（读第二页价格）被它带偏成"当前页面没有商品信息"，而且提示词变长之后模型连
JSON 格式都开始崩（`"action":{"type":"type",...}`），T2/T5/T6 一起挂。
**教训：弱模型的提示词加一条规则要付两条的代价，能不写就别写。**
正确修法是把"你刚点了什么、页面跳到哪了"当成事实回显给它（见 workflow.action_echo），
让它自己发现走错了，而不是背书式的加规则。
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是一个网页操作 Agent。你看到的是当前页面的快照文本。

第一步永远是判断：任务要的信息是否已经在【正文】里？
- 是 → 立刻输出 finish，把信息写进 answer，不要再点任何元素
- 否 → 才执行一个动作去获取

动作（每次只能一个）：
goto   {"url":"http://..."}            打开地址
click  {"target":"e3"}                 点击元素
type   {"target":"e2","text":"张三"}    输入文字（会清空原内容）
select {"target":"e4","option":"福州"}  选下拉项（填显示文字，不是 value）
scroll {"direction":"down"}            滚动页面
finish {"answer":"结果"}               任务完成

硬规则：
- target 只能用本轮快照里出现过的编号（e1、e2…），不要自己编
- 读取 / 提取 / 统计类任务不需要任何点击动作，读完正文直接 finish
- 输入框的 value 就是它的当前内容，已经填对就不要重复填
- 正文标了"已截断"说明你没看全，不要据此说"页面上没有"
- 目标不在元素列表里时先 scroll，不要猜
- 任务要分多步才能做完时，第 1 步给 plan：2~4 条、一条一个动作、写成短句。
  一步就能读完的任务不用给 plan
- 每做完一条就把它的序号补进 progress（只增不减，例如做完前两条就报 [1,2]）
- ⚠️ plan 只是提醒自己别漏步骤，**不是必须走完的清单**。
  只要正文里已经能看到任务要的答案，立刻 finish —— 哪怕 progress 还没填满

只输出 JSON，不要解释。字段：thought 一句话、plan（仅第 1 步给）、progress（已完成序号）、action。
例：{"thought":"先输入关键词","plan":["输入关键词","点搜索","读结果"],"progress":[],"action":"type","target":"e2","text":"usb"}

示例 1（提交表单）：正文末尾已出现「提交成功：姓名=张三，邮箱=zhang@example.com」
输出：{"thought":"页面已出现提交成功提示，任务完成","action":"finish","answer":"提交成功：姓名=张三，邮箱=zhang@example.com"}

示例 2（读表筛选）：正文含「无线鼠标 ¥89 机械键盘 ¥299 USB 集线器 ¥59」，任务要找价格低于 100 的
输出：{"thought":"逐条比较后，无线鼠标 89 和 USB 集线器 59 都低于 100","action":"finish","answer":"无线鼠标（¥89）、USB 集线器（¥59）"}

示例 3（搜索，注意这是连续三步，第 1 步给 plan，之后只报 progress）：
第1步（输入框 e2 的 value 是空的）：{"thought":"还没输入关键词，先输入","plan":["输入关键词 usb","点搜索按钮","读出结果条数"],"progress":[],"action":"type","target":"e2","text":"usb"}
第2步（value 已是 usb，但结果还没出来）：{"thought":"关键词已输入，点搜索按钮","progress":[1],"action":"click","target":"e3"}
第3步（正文显示「共 1 条结果」）：{"thought":"结果已更新，共 1 条","progress":[1,2,3],"action":"finish","answer":"1"}

示例 4（给了 plan，但中途发现信息已经够了，必须收尾）：
任务要公告内容，plan 是 ["打开公告页","点展开按钮读全文"]，刚打开公告页，
正文里公告全文已经在，那个"展开"按钮还没点
此时的正确输出：{"thought":"公告全文已在正文里，不需要再点展开","progress":[1],"action":"finish","answer":"本周六系统维护，暂停服务两小时"}
注意：这时候不要因为"还想多看一眼"继续点，正文里有就是有。

注意：结果条数、计数器这类文字在动作执行前可能只是个初始值（显示 0 条但还没搜过），
别拿它当答案；但动作**执行过之后**它还是那个数，那它就是真答案。
"""


# 浏览器报错 → 一句人话。
# 顺序有意义：先判"更具体的因"，再判"更笼统的果"。
# 比如视口外点击失败，报错里同时有 "outside of the viewport" 和 "Timeout"，
# 必须先命中前者——后者的修法是"再等等"，前者的修法是"换元素或先滚动"，完全相反。
_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("outside of the viewport", "元素在视口外，点不到（先 scroll，或换一个元素）"),
    ("intercepts pointer events", "元素被别的东西挡住了（可能有弹窗/遮罩）"),
    ("not visible", "元素不可见"),
    ("navigating to", "页面打开超时（网络或地址问题）"),
    ("locator.click", "点击超时（元素没出现/不可见/被挡住）"),
    ("locator.fill", "输入超时（输入框没出现）"),
    ("select_option", "下拉选择超时"),
]


def short_error(error: str, limit: int = 70) -> str:
    """把浏览器的长报错压成一句话，**只用于喂给模型**。

    为什么必须压（真实站点实测）：
    Playwright 的失败信息带一整段英文 Call log——一次点击超时是
    **1065 字符、27 行**。它会被塞进 history 里，并跟着每一步重复出现 5 次。
    对 glm-4-flash 这种"指令遵循预算极小"的模型，这等于用无关噪声把快照挤出去，
    表现就是"点一次失败之后开始乱滚"。

    边界：**trace 里永远保留完整报错**。压缩只发生在拼 prompt 这一刻。
    证据要保真，输入要精简——这两件事不该混在一起。
    """
    text = (error or "").strip()
    if not text:
        return ""

    low = text.lower()
    for pattern, phrase in _ERROR_PATTERNS:
        if pattern in low:
            if pattern == "net::err":
                # 网络错误把错误码带出来（ERR_NAME_NOT_RESOLVED / ERR_CONNECTION_REFUSED…），
                # 这才是能定位问题的信息
                idx = low.find("net::err")
                return text[idx : idx + 42]
            return phrase

    # 没认出来的：只留第一行并截断，绝不把整个调用栈灌进去
    return text.splitlines()[0][:limit]


def build_user_prompt(
    *,
    goal: str,
    step: int,
    max_steps: int,
    snapshot_text: str,
    history: list[dict],
    error_hint: str = "",
    notice: str = "",
    plan: list[str] | None = None,
    progress: list[int] | None = None,
    rejection: str = "",
) -> str:
    """拼这一轮的用户消息。

    结构：任务 → 用户否决 → 进度 → 任务分解 → 已执行历史 → 上一轮的提示 → 当前快照 → 催输出。
    历史只给最近 5 步：给太多会挤掉快照的位置，而且更早的步骤对当前决定没用。

    rejection：用户拒绝过哪些动作（P5a-4）。空字符串时**整段不渲染**——
    这一点是硬要求，不是优化：M5a 回放器拿历史 trace 重建 prompt 时不会传它，
    默认空就保证历史基线一个字都不变（P5a-4 之前所有回放结果仍然可比）。
    """
    parts: list[str] = [f"任务：{goal}"]

    if rejection:
        # 紧贴在"任务"后面、**连进度行都排在它之后**。
        # 为什么（P5a-4 实测踩出来的，血的教训）：
        # 那个任务的目标本身写着"然后点击提交按钮"，而拒绝理由只作为一次性 notice
        # 出现在末尾。结果模型**连续 5 轮**都重新点提交——每拒一次它就换一步再试。
        # 它不是没看见那句 notice，而是把"任务"当成了更高优先级的指令：
        # 目标每轮都完整重读一遍，而 notice 只是末尾一句"观察"。
        # 所以否决必须摆到和目标同级、甚至更靠前的位置，并且**只要发生过就每轮都带上**，
        # 不能只在下一轮说一次。弱模型对"位置"极其敏感，这一段的位置就是它的权重。
        parts.append(f"[用户否决] {rejection}")

    parts.append(f"[进度] 第 {step}/{max_steps} 步")

    # [任务分解] 必须在快照之前：模型是靠这一块决定"还剩哪条没做"，
    # 放在最后会被长长的正文冲淡（实测弱模型只看最近的内容）
    if plan:
        done = set(progress or [])
        lines = ["[任务分解]"]
        for index, item in enumerate(plan, start=1):
            mark = "（已完成）" if index in done else "（未完成）"
            lines.append(f"  {index}. {item}{mark}")
        # 再补一句"还剩几条"：这一句才是真正拦住"多点一步"的东西。
        # 实测 R3 的失败形态是"第一条已经读到了，但它写'需要点 Next 查看下一页'"——
        # 逐条打勾它看得懂，但不会自己去数"还剩几条"，于是永远觉得还差一步。
        left = [i for i in range(1, len(plan) + 1) if i not in done]
        if left:
            # 这里刻意不说"全部完成后才能 finish"。
            # 实测过一次：写成命令句（"全部完成后立刻 finish"）时，模型会把它当成硬闸门——
            # R1 那题它第 1 步 thought 里已经写出了正确答案，却因为 plan 还剩一条，
            # 选择继续点进作者页，把一道一步题走成了 6 步 loop。
            # plan 只能当提醒，不能当闸门；真正的闸门永远是"正文里有没有答案"。
            lines.append(
                f"还剩 {len(left)} 条未完成。plan 只是提醒，不是必须走完的清单："
                "现在就能回答任务的话，直接 finish。"
            )
        else:
            lines.append("已全部完成，立刻 finish，不要再点任何元素。")
        parts.append("\n".join(lines))
    elif step <= 2:
        # 前两步还没给 plan 就轻推一下。
        # 为什么限 step<=2：一步就能做完的任务（读正文直接答）本来就不需要 plan，
        # 每步都催会变成噪音，还会把模型推向"先做点什么"。
        parts.append("[任务分解] 还没有。如果这个任务要多步才能做完，请在本轮输出里补上 plan。")

    if history:
        lines = ["[已执行]"]
        for item in history[-5:]:
            # 错误要压缩，理由见 short_error 的注释（1065 字符的 Call log 会挤爆上下文）
            status = "成功" if item.get("ok") else f"失败：{short_error(item.get('error') or '')}"
            lines.append(f"  第{item['step']}步 {item['action']} → {status}")
        parts.append("\n".join(lines))

    if error_hint:
        # 上一轮动作被拒了，必须明确告诉它错在哪，否则它会原样再犯一次
        parts.append(f"[上一步被拒绝] {error_hint}\n请换一个合法动作。")

    if notice:
        # 主动告诉它"你刚才那下白费了"，比等它自己发现要早好几步
        parts.append(f"[观察] {notice}")

    parts.append(snapshot_text)
    parts.append("输出下一步 JSON：")
    return "\n\n".join(parts)
