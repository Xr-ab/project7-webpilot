"""成本口径脚本的单测：只测"钱怎么换算"这类**错了看不出来**的地方。

为什么值得单独测（踩过的）：
第一版 `_fmt_yuan` 把元换分写成了 ×1000（正确是 ×100），
0.000471 元被印成「0.471 分」（真值 0.0471 分）—— 表格里所有成本都大了 10 倍，
而报告读起来**毫无异常**：没有报错、没有负数、数量级也"看着像钱"。
这种错只能靠手算验算或单测抓，靠眼睛看不出来。

脚本放在 scripts/ 而不是 backend/app/（它是运维/汇报工具，不进服务进程），
所以这里手动把 scripts/ 加进 sys.path 再导入——不为了测试去改它的位置。
"""
from __future__ import annotations

import sys
from pathlib import Path

# backend/tests/ → backend/ → 项目根 → scripts/
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cost_report  # noqa: E402


def test_yuan_to_fen_is_times_100_not_1000():
    """0.000471 元 = 0.0471 分。这条就是那个 10 倍 bug 的回归。"""
    assert cost_report._fmt_yuan(0.000471) == "0.047 分"


def test_fmt_yuan_switches_unit_by_magnitude():
    """量级不同要换单位，否则表格里全是 0.0004 这种看不出差异的数。"""
    assert cost_report._fmt_yuan(0) == "0 元"
    assert cost_report._fmt_yuan(0.0005) == "0.050 分"     # 半分钱
    assert cost_report._fmt_yuan(0.0135) == "0.0135 元"     # 超过一分就回到元
    assert cost_report._fmt_yuan(1.0) == "1.0000 元"


def test_fmt_yuan_keeps_absolute_value_below_one_thousandth_fen():
    """小到 0.001 分以下就不换单位了——印成"0.000 分"等于丢信息。"""
    text = cost_report._fmt_yuan(0.000001)
    assert text.endswith("元")
    assert "0.000 分" not in text


def test_prices_are_ascending_by_tier():
    """三个折算档位的顺序必须是便宜的在前：flashx < air < plus。

    写反了不会报错，只会让报告里的"等价成本"莫名其妙地大或小。
    """
    assert cost_report.PRICES["flashx"][0] < cost_report.PRICES["air"][0]
    assert cost_report.PRICES["air"][0] < cost_report.PRICES["plus"][0]


def test_every_price_has_a_human_readable_source():
    """折算基准必须写清"拿什么模型折的"——只给一个数字没法对账。"""
    for key, (price, name) in cost_report.PRICES.items():
        assert price > 0, key
        assert "GLM" in name, f"{key} 的折算基准没写清模型名"
