"""
LangGraph Agent 图构建与全局单例管理

认知图结构（启用路由时）：
    START
      │
      ▼
    route_model        ← 判断意图（chat/code/search/search_code）
      │
      ▼
    retrieve_context   ← 检索 RAG + 组装历史消息
      │
      ▼
    planner            ← 生成执行计划（search/search_code 路由触发）
      │
      ▼
    call_model         ← LLM 推理（当前步骤）
      │
    should_continue?
      ├── "tools"      ← ToolNode 并发执行工具
      │      │
      │      ▼
      │   call_model_after_tool  ← 工具后 LLM 综合（answer_model）
      │      │
      │   should_continue_after_tool?
      │      ├── "tools"         ← 继续调用更多工具
      │      └── "reflector"     ← 评估步骤完成情况
      │
      └── "reflector"  ← 无工具调用时直接评估
               │
           reflector_routing?
               ├── "call_model"    ← 继续下一步（continue/retry）
               └── "save_response" ← 所有步骤完成
                        │
                        ▼
                   compress_memory
                        │
                        ▼
                       END

无计划时（chat/code 路由）：
    call_model → (无工具) → save_response → compress_memory → END
    call_model → tools → call_model_after_tool → save_response → compress_memory → END
"""
import logging
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from config import CHAT_MODEL, ROUTER_ENABLED
from graph.edges import should_continue, should_continue_after_tool, reflector_routing
from graph.nodes import (
    compress_memory,
    make_call_model,
    make_call_model_after_tool,
    make_planner,
    make_reflector,
    make_retrieve_context,
    route_model,
    save_response,
)
from graph.state import GraphState

logger = logging.getLogger("graph.agent")

_graph_cache: dict[str, Any] = {}
_tools: list[BaseTool] = []


def build_graph(tools: list[BaseTool], model: str = CHAT_MODEL) -> Any:
    tool_names = [t.name for t in tools]

    retrieve_fn = make_retrieve_context(tool_names)
    planner_fn = make_planner()
    call_model_fn = make_call_model(tools)
    call_model_tool_fn = make_call_model_after_tool(tools)
    reflector_fn = make_reflector()

    graph = StateGraph(GraphState)

    # ── 注册节点 ────────────────────────────────────────────────────────────
    graph.add_node("retrieve_context", retrieve_fn)
    graph.add_node("planner", planner_fn)
    graph.add_node("call_model", call_model_fn)
    graph.add_node("call_model_after_tool", call_model_tool_fn)
    graph.add_node("reflector", reflector_fn)
    graph.add_node("save_response", save_response)
    graph.add_node("compress_memory", compress_memory)

    # ── 工具节点 ────────────────────────────────────────────────────────────
    if tools:
        tool_node = ToolNode(tools)
        graph.add_node("tools", tool_node)

        # call_model → tools 或 reflector（或 save_response 无计划时）
        graph.add_conditional_edges(
            "call_model",
            should_continue,
            {"tools": "tools", "reflector": "reflector", "save_response": "save_response"},
        )

        # tools → call_model_after_tool
        graph.add_edge("tools", "call_model_after_tool")

        # call_model_after_tool → tools 或 reflector（或 save_response）
        graph.add_conditional_edges(
            "call_model_after_tool",
            should_continue_after_tool,
            {"tools": "tools", "reflector": "reflector", "save_response": "save_response"},
        )
    else:
        # 无工具：call_model → reflector 或 save_response
        graph.add_conditional_edges(
            "call_model",
            should_continue,
            {"reflector": "reflector", "save_response": "save_response"},
        )

    # ── reflector 路由：继续执行 or 保存 ────────────────────────────────────
    graph.add_conditional_edges(
        "reflector",
        reflector_routing,
        {"call_model": "call_model", "save_response": "save_response"},
    )

    # ── 线性边 ─────────────────────────────────────────────────────────────
    graph.add_edge("retrieve_context", "planner")
    graph.add_edge("planner", "call_model")
    graph.add_edge("save_response", "compress_memory")
    graph.add_edge("compress_memory", END)

    # ── 入口（路由模式 vs 直连） ─────────────────────────────────────────────
    if ROUTER_ENABLED:
        graph.add_node("route_model", route_model)
        graph.add_edge(START, "route_model")
        graph.add_edge("route_model", "retrieve_context")
    else:
        graph.add_edge(START, "retrieve_context")

    return graph.compile()


def init(tools: list[BaseTool], model: str = CHAT_MODEL) -> None:
    """应用启动时调用，编译并缓存图。"""
    global _tools
    _tools = tools
    _graph_cache["default"] = build_graph(tools, model)


def get_graph(model: str = CHAT_MODEL) -> Any:
    """返回已编译的图。路由模式下所有请求共用同一张图。"""
    if "default" not in _graph_cache:
        raise RuntimeError("Agent 图未初始化，请先调用 graph.agent.init(tools)")
    return _graph_cache["default"]
