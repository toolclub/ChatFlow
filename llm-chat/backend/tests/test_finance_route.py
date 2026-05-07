"""finance 路由 + 路由专用 system prompt — 单元测试

验证：
  1. route_node 路由候选包含 finance（标签匹配优先级正确）
  2. context_builder 在 route=finance 时把 prompts/routes/finance.md 注入到 SystemMessage
  3. 普通路由（chat/search/code/search_code）不会注入 finance prompt
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
def test_route_candidates_include_finance():
    """T-FINANCE-ROUTE-01：路由解析候选包含 finance，且优先级正确。"""
    from graph.nodes.route_node import _ROUTE_CANDIDATES

    assert "finance" in _ROUTE_CANDIDATES
    # search_code 必须在 search 之前，否则部分匹配
    idx = list(_ROUTE_CANDIDATES)
    assert idx.index("search_code") < idx.index("search")


@pytest.mark.unit
def test_context_builder_injects_finance_prompt():
    """T-FINANCE-ROUTE-02：route='finance' 时把 routes/finance.md 注入到 SystemMessage。"""
    from langchain_core.messages import SystemMessage

    from memory.context_builder import build_messages
    from memory.schema import Conversation
    from prompts import load_prompt

    conv = Conversation(id="test_conv", title="t", system_prompt="")
    msgs = build_messages(conv, route="finance")
    assert msgs and isinstance(msgs[0], SystemMessage)

    sys_text = msgs[0].content
    finance_prompt = load_prompt("routes/finance")
    assert "金融分析模式" in sys_text
    # 关键约束句必须存在
    assert "不构成投资建议" in sys_text
    # 完整 prompt 嵌入（不被截断）
    assert finance_prompt[:80] in sys_text


@pytest.mark.unit
def test_context_builder_skips_finance_prompt_for_chat():
    """T-FINANCE-ROUTE-03：非 finance 路由不应注入 finance prompt。"""
    from langchain_core.messages import SystemMessage

    from memory.context_builder import build_messages
    from memory.schema import Conversation

    conv = Conversation(id="test_conv2", title="t", system_prompt="")
    msgs = build_messages(conv, route="chat")
    assert msgs and isinstance(msgs[0], SystemMessage)
    assert "金融分析模式" not in msgs[0].content
    assert "不构成投资建议" not in msgs[0].content
