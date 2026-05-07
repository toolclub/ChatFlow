"""finance 工具按 route 隔离 — 单元测试

验证：
  1. 7 个 finance 工具都注册成功且带 "finance" tag
  2. filter_tools_by_route 在非 finance 路由下剔除 finance 工具
  3. finance 路由下保留所有工具
"""
from __future__ import annotations

import pytest


def _make_fake_tool(name: str):
    """构造最小可测的 BaseTool（不调真实接口）。"""
    from langchain_core.tools import tool

    @tool
    async def _f(x: str) -> str:
        """noop"""
        return x

    _f.name = name
    return _f


@pytest.mark.unit
def test_finance_tools_have_finance_tag():
    """T-FINANCE-TOOL-01：所有 finance 子目录工具都带 'finance' tag。"""
    from tools import discover, get_tool_tags

    discover("tools.builtin")
    discover("tools.builtin.finance")

    expected = {
        "stock_quote", "stock_kline", "stock_news",
        "stock_fundamentals", "stock_money_flow",
        "industry_overview", "macro_snapshot",
    }
    for name in expected:
        tags = get_tool_tags(name)
        assert "finance" in tags, f"{name} 缺 finance tag，实际 tags={tags}"


@pytest.mark.unit
def test_filter_tools_by_route_excludes_finance_for_chat():
    """T-FINANCE-TOOL-02：route='chat' 时 finance 工具被剔除。"""
    from tools import discover, filter_tools_by_route, get_all_tools

    discover("tools.builtin")
    discover("tools.builtin.finance")

    tools = get_all_tools()
    filtered = filter_tools_by_route(tools, "chat")
    names = {t.name for t in filtered}

    finance_names = {
        "stock_quote", "stock_kline", "stock_news",
        "stock_fundamentals", "stock_money_flow",
        "industry_overview", "macro_snapshot",
    }
    assert names.isdisjoint(finance_names), f"chat 路由不应有 finance 工具: {names & finance_names}"

    # 普通工具仍然在
    assert "calculator" in names
    assert "web_search" in names


@pytest.mark.unit
def test_filter_tools_by_route_keeps_finance_for_finance_route():
    """T-FINANCE-TOOL-03：route='finance' 时全部工具保留。"""
    from tools import discover, filter_tools_by_route, get_all_tools

    discover("tools.builtin")
    discover("tools.builtin.finance")

    tools = get_all_tools()
    filtered = filter_tools_by_route(tools, "finance")
    names = {t.name for t in filtered}

    finance_names = {
        "stock_quote", "stock_kline", "stock_news",
        "stock_fundamentals", "stock_money_flow",
        "industry_overview", "macro_snapshot",
    }
    assert finance_names.issubset(names), f"finance 路由应包含所有 finance 工具: 缺 {finance_names - names}"
