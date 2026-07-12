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



# 现存的 finance 工具（单一真相源）。
#
# stock_fundamentals / stock_money_flow / industry_overview / macro_snapshot 四个模块已在
# v2 删除（设计文档 §4.3）：它们 import 的 `quant.data` 子模块（cache/news/macro/industry…）
# 在本仓库**从未存在过** —— 启动日志里那条
#     「跳过模块 tools.builtin.finance.stock_news（导入失败）: No module named 'quant.data'」
# 就是它们，四个工具一次都没被成功加载过。v2 新建了真正的 quant/data/ 包（announcement.py /
# sec_edgar.py）并借此复活了 stock_news；另外四个是死代码，删掉而不是留着假装存在。
FINANCE_TOOL_NAMES = {"stock_quote", "stock_kline", "stock_news"}


@pytest.mark.unit
def test_finance_tools_have_finance_tag():
    """T-FINANCE-TOOL-01：所有 finance 子目录工具都带 'finance' tag。"""
    from tools import discover, get_tool_tags

    discover("tools.builtin")
    discover("tools.builtin.finance")

    for name in FINANCE_TOOL_NAMES:
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

    finance_names = FINANCE_TOOL_NAMES
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

    finance_names = FINANCE_TOOL_NAMES
    assert finance_names.issubset(names), f"finance 路由应包含所有 finance 工具: 缺 {finance_names - names}"
