"""snapshot.py 的单元测试。

验收线（来自 P1 计划）：5 个测试页各产出一份快照，人工核对每个编号能定位到真实元素。
"人工核对"没法自动化，但"每个编号确实能定位到唯一元素"可以——这就是本文件的核心断言。
"""
from __future__ import annotations

import pytest

from app.browser.snapshot import (
    ID_ATTR,
    render_snapshot_text,
    selector_for,
    take_snapshot,
)

from .conftest import ALL_PAGES, open_page


async def test_every_page_ids_resolve_to_real_element(session):
    """每个页面：编号唯一、且每个编号都能在 DOM 里定位到唯一一个元素。

    这条测试是整个 M3 的地基。编号定位不到，Agent 拿到的就只是一堆代号，
    后面的动作执行全是空转。
    """
    for name in ALL_PAGES:
        await open_page(session, name)
        snap = await take_snapshot(session.page)

        assert snap["title"], f"{name}: 标题为空"
        assert snap["elements_total"] > 0, f"{name}: 一个可交互元素都没找到"
        assert "file://" in snap["url"], f"{name}: url 不对 -> {snap['url']}"

        ids = [el["id"] for el in snap["elements"]]
        assert len(ids) == len(set(ids)), f"{name}: 编号重复 -> {ids}"

        for eid in ids:
            count = await session.page.locator(selector_for(eid)).count()
            assert count == 1, f"{name}: 编号 {eid} 定位到 {count} 个元素（应为 1）"


async def test_hidden_and_disabled_are_filtered_out(session):
    """不可见元素、禁用按钮不该出现在快照里。

    index.html 里专门放了两个"陷阱"：display:none 的链接、disabled 的按钮。
    它们要是漏进清单，Agent 就会去点一个点不到的东西——这是浏览器 Agent 的经典翻车方式。
    """
    await open_page(session, "index.html")
    snap = await take_snapshot(session.page)

    texts = [el["text"] for el in snap["elements"]]
    assert not any("隐藏" in t for t in texts), f"隐藏链接泄漏进快照：{texts}"
    assert not any("不可用" in t for t in texts), f"禁用按钮泄漏进快照：{texts}"

    # 首页真正可点的只有 6 个导航链接：列表 / 表单 / 搜索 / 详情 / 我的账户 / 登录。
    # P5b 之前是 4 个——多了账户和登录两个入口，这条断言跟着改是**应该的**：
    # 测试站的导航变了，快照结果就该跟着变。断言死守住旧数字，等于逼测试站不许长。
    assert snap["elements_total"] == 6, f"预期 6 个可交互元素，实际 {snap['elements_total']}"
    assert all("隐藏" not in t and "不可用" not in t for t in texts), texts


async def test_form_fields_get_human_readable_names(session):
    """表单字段要拿到 <label> 里的中文名，而不是空字符串或一长串选项文字。

    名字错了模型就认不出这个框是干嘛的（"姓名" vs "城市 请选择 福州 厦门 杭州"）。
    """
    await open_page(session, "form.html")
    snap = await take_snapshot(session.page)
    by_text = {el["text"]: el for el in snap["elements"]}

    assert "姓名" in by_text, f"没拿到姓名输入框：{list(by_text)}"
    assert by_text["姓名"]["role"] == "input"

    assert "城市" in by_text, f"下拉框名字不对：{list(by_text)}"
    city = by_text["城市"]
    assert city["role"] == "select"
    assert city["selected_text"] == "请选择"

    assert "订阅产品更新" in by_text, f"勾选框名字不对：{list(by_text)}"
    assert by_text["订阅产品更新"]["checked"] is False

    assert "提交" in by_text
    assert by_text["提交"]["role"] == "button"


async def test_input_value_is_reflected_in_next_snapshot(session):
    """填过内容后再快照，必须能看到 value。

    这是防死循环的关键：Agent 看到"姓名 已经是张三"就不会再填第二遍。
    """
    await open_page(session, "form.html")
    before = await take_snapshot(session.page)
    name_el = next(el for el in before["elements"] if el["text"] == "姓名")
    assert name_el.get("value", "") == ""

    await session.page.fill(selector_for(name_el["id"]), "张三")

    after = await take_snapshot(session.page)
    name_after = next(el for el in after["elements"] if el["text"] == "姓名")
    assert name_after["value"] == "张三"


async def test_ids_are_stable_between_two_snapshots(session):
    """同一页面连拍两次，编号必须完全一致。

    编号每轮乱跳的话，模型上一轮看到的 e3 这一轮变成了别的元素，
    它做出的决策就会作用到错的地方。
    """
    await open_page(session, "list.html")
    first = [el["id"] for el in (await take_snapshot(session.page))["elements"]]
    second = [el["id"] for el in (await take_snapshot(session.page))["elements"]]
    assert first == second


async def test_dynamic_content_after_click(session):
    """点一下筛选按钮，页面内容变化要能被快照捕捉到（JS 渲染的 DOM 也算）。"""
    await open_page(session, "list.html")
    snap = await take_snapshot(session.page)
    keyword = next(el for el in snap["elements"] if el["role"] == "input")

    await session.page.fill(selector_for(keyword["id"]), "usb")
    btn = next(el for el in snap["elements"] if el["role"] == "button")
    await session.page.click(selector_for(btn["id"]))

    after = await take_snapshot(session.page)
    # 大小写不敏感已经修过：输入 usb 应该命中 USB 集线器
    assert "USB 集线器" in after["text"]
    assert "无线鼠标" not in after["text"]


def test_render_text_contains_key_info():
    """渲染成文本时，URL/标题/正文/元素都要在，截断要有明确提示（纯函数，不启浏览器）。"""
    snap = {
        "url": "http://127.0.0.1:8000/testsite/index.html",
        "title": "WebPilot 测试站",
        "headings": ["WebPilot 本地测试站"],
        "text": "很长的正文" * 10,
        "text_truncated": True,
        "elements": [
            {"id": "e1", "role": "link", "text": "产品列表", "href": "list.html", "in_viewport": True},
            {"id": "e2", "role": "input", "text": "关键词", "value": "usb", "in_viewport": False},
        ],
        "elements_total": 2,
        "elements_shown": 2,
        "scroll": {"y": 0, "viewport": 800, "page_height": 1600},
    }
    text = render_snapshot_text(snap)

    assert "http://127.0.0.1:8000/testsite/index.html" in text
    assert "WebPilot 测试站" in text
    assert "已截断" in text
    assert "e1" in text and "产品列表" in text and "list.html" in text
    assert "value='usb'" in text
    # 视口外要显式标出来（曾经用 * 标记"视口内"，模型看不懂图例，真实站点踩过）
    assert "[屏外]" in text
    # 视口内的那一条不该带标记
    line_e1 = next(line for line in text.splitlines() if "产品列表" in line)
    assert "[屏外]" not in line_e1


async def test_long_text_keeps_head_and_tail(session):
    """正文超长时要"头尾都留"。

    只留头会把页面底部的关键结论（合计、"共 N 条结果"、提交成功提示）全丢掉，
    模型就会把"没看到"当成"不存在"。用 set_content 现造一个长页面，不用真的改测试站。
    """
    head_marker = "开头标记AAA"
    tail_marker = "结尾标记ZZZ"
    html = (
        "<html><body><p>" + head_marker + "</p>"
        "<p>" + ("填充内容 " * 200) + "</p>"
        "<p>" + tail_marker + "</p></body></html>"
    )
    await session.page.set_content(html)

    snap = await take_snapshot(session.page, max_text_chars=200)

    assert snap["text_truncated"] is True
    assert head_marker in snap["text"], "头部内容丢了"
    assert tail_marker in snap["text"], "尾部内容丢了（关键结论常在页面底部）"
    assert "中间省略" in snap["text"], "省略处没有标记，模型会以为看全了"


async def test_elements_truncation_tells_model_to_scroll(session):
    """元素数量超上限时，文本里必须告诉模型"还能滚动看更多"。

    否则第 90 个元素对模型就等于不存在，它只会在前 80 个里瞎猜或者干脆放弃。
    """
    links = "".join(f'<a href="#{i}">链接{i}</a>' for i in range(1, 6))
    await session.page.set_content(f"<html><body>{links}</body></html>")

    snap = await take_snapshot(session.page, max_elements=2)
    assert snap["elements_total"] == 5
    assert snap["elements_shown"] == 2

    rendered = render_snapshot_text(snap)
    assert "共 5 个" in rendered
    assert "scroll" in rendered


@pytest.mark.parametrize("bad_id", ["", "e", "e9999", "e1; rm -rf /", "e1\" onmouseover=\"x", "E1"])
def test_selector_for_rejects_bad_ids(bad_id):
    """模型返回的编号必须先过格式校验，不能直接拼进选择器。"""
    with pytest.raises(ValueError):
        selector_for(bad_id)

def test_selector_for_accepts_valid_id():
    assert selector_for("e7") == f'[{ID_ATTR}="e7"]'


async def test_modal_open_lists_only_modal_elements(session):
    """弹窗开着时，只能列弹窗里的元素。

    真实站点（MDN 搜索）实测：搜索弹窗打开后，整页元素仍然全列给模型，
    它去点被遮住的 JavaScript 链接，Playwright 一直报
    「<mdn-search-modal> intercepts pointer events」，重试到超时，6 步全废。
    弹窗外的元素此刻**真的点不到**，列给模型就是给陷阱。
    """
    await session.page.set_content(
        "<html><body>"
        "<a href='#a'>外面的链接 A</a>"
        "<a href='#b'>外面的链接 B</a>"
        "<dialog open><input type='search' placeholder='Search'>"
        "<a href='#inner'>弹窗里的链接</a></dialog>"
        "</body></html>"
    )
    snap = await take_snapshot(session.page)

    assert snap["modal_open"] is True
    texts = [el["text"] for el in snap["elements"]]
    assert "弹窗里的链接" in texts
    assert "外面的链接 A" not in texts
    assert "外面的链接 B" not in texts
    # 输入框必须在：弹窗里唯一能输入的入口
    assert any(el["role"] in ("input", "searchbox", "combobox") for el in snap["elements"])

    rendered = render_snapshot_text(snap)
    assert "弹窗" in rendered and "点不到" in rendered


async def test_no_modal_keeps_full_list(session):
    """没有弹窗时不能误判——否则正常页面会被砍成一个元素。"""
    await session.page.set_content(
        "<html><body><a href='#1'>链接一</a><a href='#2'>链接二</a></body></html>"
    )
    snap = await take_snapshot(session.page)
    assert snap["modal_open"] is False
    assert len(snap["elements"]) == 2


async def test_bbox_is_collected_and_normalized(session):
    """bbox 采集：每个元素带 {x, y, w, h}，视口内元素归一化在 0~1。

    P6 视觉兜底的地基（A1）：没有坐标，视觉模型说「图里左上角的按钮」
    就无法对回 e3 这样的编号——元素↔像素之间永远缺这座桥。
    为什么归一化而不是存像素：截图尺寸随视口变，比值不变，
    同一份 trace 换个分辨率回放仍然可解释。
    """
    await open_page(session, "index.html")
    snap = await take_snapshot(session.page)

    assert snap["elements"], "页面上应该有元素"
    for el in snap["elements"]:
        bbox = el.get("bbox")
        assert bbox is not None, f"{el['id']} 缺 bbox"
        assert set(bbox) == {"x", "y", "w", "h"}, f"{el['id']} bbox 字段不全：{bbox}"
        for v in bbox.values():
            assert isinstance(v, (int, float)), f"{el['id']} bbox 值不是数字：{v}"
        # 本测试站没有滚动，所有元素都在视口内：四个值都应落在 0~1
        if el.get("in_viewport") is True:
            assert 0 <= bbox["x"] <= 1, f"{el['id']} x 越界：{bbox}"
            assert 0 <= bbox["y"] <= 1, f"{el['id']} y 越界：{bbox}"
            assert 0 < bbox["w"] <= 1, f"{el['id']} w 越界：{bbox}"
            assert 0 < bbox["h"] <= 1, f"{el['id']} h 越界：{bbox}"

    # 不同位置的元素 bbox 应该不同（两个导航链接不会叠在同一处）——
    # 防止 JS 写错导致所有元素拿到同一个（比如页面根节点的）包围盒
    boxes = [tuple(el["bbox"].values()) for el in snap["elements"]]
    assert len(set(boxes)) == len(boxes), f"bbox 全都一样，疑似算错：{boxes}"


async def test_bbox_never_reaches_the_model(session):
    """`bbox` 绝不能出现在给模型看的文本里（纪律同 input_type）。

    input_type 教过的道理原样适用：bbox 是 P6 视觉模块的内部字段，
    模型不需要坐标也能点元素（它靠编号），而一旦 render_snapshot_text
    渲染了它，历史 trace（没有这个字段）重放的 prompt 就和当年不一样——
    M5a 回放基线静默失真，且不会有任何测试红。
    这条测试拦的是以后那个「顺手把坐标也列上」的人。
    """
    await open_page(session, "login.html")
    snap = await take_snapshot(session.page)

    # 前提确认：页面上真的采到了 bbox，否则"没泄漏"只是因为压根没有
    assert all("bbox" in el for el in snap["elements"])

    rendered = render_snapshot_text(snap)
    assert "bbox" not in rendered, "字段名泄漏进 prompt 了"


async def test_input_type_never_reaches_the_model(session):
    """`input_type` 绝不能出现在给模型看的文本里。

    为什么这条值得单独一个测试（它看起来只是个字段）：
    `input_type` 是 **P5 审批闸门**判断"这一步是不是在填密码"的依据，
    纯内部字段（见 `app/agent/approval.py` 的模块说明）。它对模型没有任何用处——
    模型看到「密码 [input=password]」也做不了别的事，只是多几个 token。

    但真正的代价在别处：**它会让 M5a 回放基线静默失真**。
    回放器拿历史 trace 里的快照重放 prompt，而那些快照是 P5 之前存的、没有这个字段。
    一旦这里渲染了它，新 prompt ≠ 当时实际发出去的 prompt，
    回放对比出来的差异就不再是"模型变了"，而是"prompt 变了"——结论直接作废。
    最麻烦的是**不会有任何测试红**：回放照样能跑，只是数字悄悄偏了。

    所以这条测试的价值不在"现在是对的"，而在**拦住以后那个"顺手把它加上去"的人**。
    """
    await open_page(session, "login.html")
    snap = await take_snapshot(session.page)

    # 先确认这个页面上真的有密码框——否则"没渲染"可能只是因为压根没这个元素，
    # 测试就成了永远绿灯的摆设。
    types = [el.get("input_type") for el in snap["elements"]]
    assert "password" in types, f"login.html 上应该采到密码框，实际类型：{types}"

    rendered = render_snapshot_text(snap)
    assert "input_type" not in rendered, "字段名泄漏进 prompt 了"
    assert "password" not in rendered.lower(), (
        "密码框的 DOM 类型泄漏进 prompt 了——见本测试的 docstring，这会污染回放基线"
    )
