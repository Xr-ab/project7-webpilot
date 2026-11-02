"""动作校验的单元测试：不启浏览器、不调模型，纯逻辑。

这一层测试的价值：模型输出千奇百怪，但校验闸门只有一处。
把闸门测透了，循环里剩下的失败就一定是"页面/模型"的问题，不会是我们自己的漏洞。
"""
from __future__ import annotations

import pytest

from app.agent.actions import (
    ActionError,
    ActionType,
    check_action,
    describe,
    parse_raw,
)

# 造一份最小快照元素表：链接、输入框、下拉框各一个，够覆盖 role 校验
ELEMENTS = {
    "e1": {"id": "e1", "role": "link", "text": "产品列表"},
    "e2": {"id": "e2", "role": "input", "text": "姓名"},
    "e4": {"id": "e4", "role": "select", "text": "城市"},
}
HOSTS = ("127.0.0.1", "localhost")


def check(payload: dict, *, elements: dict | None = None, hosts: tuple = HOSTS):
    """薄封装：省掉每个测试都写 parse_raw + check_action。"""
    return check_action(
        parse_raw(payload),
        elements=ELEMENTS if elements is None else elements,
        allowed_hosts=hosts,
    )


def test_click_valid():
    action = check({"thought": "点列表", "action": "click", "target": "e1"})
    assert action.action is ActionType.CLICK
    assert action.target == "e1"
    assert action.thought == "点列表"


def test_click_rejects_invented_id():
    """模型编造编号是最常见的失败，错误信息里必须给出可用编号，它才知道怎么改。"""
    with pytest.raises(ActionError) as exc:
        check({"action": "click", "target": "e99"})
    msg = str(exc.value)
    assert "不在本轮快照里" in msg
    assert "e1" in msg and "e4" in msg


def test_click_requires_target():
    with pytest.raises(ActionError, match="缺少 target"):
        check({"action": "click"})


def test_type_valid():
    action = check({"action": "type", "target": "e2", "text": "张三"})
    assert action.action is ActionType.TYPE
    assert action.text == "张三"


def test_type_rejects_empty_text():
    with pytest.raises(ActionError, match="不能为空"):
        check({"action": "type", "target": "e2", "text": "   "})


def test_type_rejects_overlong_text():
    with pytest.raises(ActionError, match="过长"):
        check({"action": "type", "target": "e2", "text": "字" * 201})


def test_type_rejects_non_input_target():
    """在链接上打字是模型理解错了元素类型，直接拒掉比让它点空更好。"""
    with pytest.raises(ActionError, match="input"):
        check({"action": "type", "target": "e1", "text": "张三"})


def test_select_valid():
    action = check({"action": "select", "target": "e4", "option": "福州"})
    assert action.action is ActionType.SELECT
    assert action.option == "福州"


def test_select_rejects_missing_option():
    with pytest.raises(ActionError, match="缺少 option"):
        check({"action": "select", "target": "e4"})


def test_select_rejects_wrong_role():
    with pytest.raises(ActionError, match="不是下拉框"):
        check({"action": "select", "target": "e2", "option": "福州"})


def test_scroll_defaults_to_down():
    action = check({"action": "scroll"})
    assert action.direction == "down"


def test_scroll_rejects_bad_direction():
    with pytest.raises(ActionError, match="up 或 down"):
        check({"action": "scroll", "direction": "left"})


def test_goto_valid():
    action = check({"action": "goto", "url": "http://127.0.0.1:8000/testsite/list.html"})
    assert action.action is ActionType.GOTO


def test_goto_rejects_external_host():
    """白名单是硬底线：能访问外网就等于把内网和外部世界打通了。"""
    with pytest.raises(ActionError, match="不在白名单"):
        check({"action": "goto", "url": "https://example.com/a"})


def test_goto_rejects_non_http_scheme():
    """file:// 能读本地文件，javascript: 更是直接执行脚本，必须挡掉。"""
    with pytest.raises(ActionError, match="http"):
        check({"action": "goto", "url": "file:///C:/Windows/win.ini"})
    with pytest.raises(ActionError, match="http"):
        check({"action": "goto", "url": "javascript:alert(1)"})


def test_finish_valid():
    action = check({"action": "finish", "answer": "共 6 件商品"})
    assert action.action is ActionType.FINISH
    assert action.answer == "共 6 件商品"


def test_finish_requires_answer():
    with pytest.raises(ActionError, match="缺少 answer"):
        check({"action": "finish"})


def test_unknown_action_rejected():
    with pytest.raises(ActionError, match="未知动作"):
        check({"action": "submit"})


def test_empty_action_rejected():
    with pytest.raises(ActionError, match="未知动作"):
        check({})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"action": "goto", "url": "http://127.0.0.1:8000/x"}, "goto http://127.0.0.1:8000/x"),
        ({"action": "click", "target": "e1"}, "click e1"),
        ({"action": "type", "target": "e2", "text": "张三"}, "type e2 <- '张三'"),
        ({"action": "select", "target": "e4", "option": "福州"}, "select e4 <- 福州"),
        ({"action": "scroll", "direction": "up"}, "scroll up"),
        ({"action": "finish", "answer": "好了"}, "finish '好了'"),
    ],
)
def test_describe_is_readable(payload, expected):
    """trace 里存的是这句话，人排查时要一眼看懂。"""
    assert describe(check(payload)) == expected


def test_parse_raw_error_shows_correct_shape():
    """报错必须把正确形状摆出来（P7-D1 T6 实测）。

    模型把 action 写成了对象：{"action":{"action":"type","target":"e1","text":"李四"}}。
    原来只回一句英文 "Input should be a valid string"，模型看不懂该改成什么样，
    原样重发一次就撞上 invalid_action。所以报错里必须带可照抄的样例。
    """
    bad = {"thought": "填姓名", "action": {"action": "type", "target": "e1", "text": "李四"}}
    with pytest.raises(ActionError) as err:
        parse_raw(bad)
    msg = str(err.value)
    assert "动作格式不对" in msg
    # 要能照抄的形状：action 是字符串，参数平铺
    assert '"action":"type"' in msg
    assert "不要" in msg
