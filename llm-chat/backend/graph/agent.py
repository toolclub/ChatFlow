"""
LangGraph Agent 图构建与全局单例管理

图结构：
    START
      │
      ▼
    route_model        ← qwen3:1.7b 判断意图，覆盖 state.model（可关闭）
      │
      ▼
    retrieve_context   ← 检索 RAG + 判断 forget_mode + 组装历史消息
      │
      ▼
    call_model         ← LLM 推理（已 bind_tools，模型由 state.model 决定）
      │
    should_continue?
      ├── "tools"      ← ToolNode 并发执行工具 → 回到 call_model
      └── "save_response"
              │
              ▼
         compress_memory  ← 按需生成摘要 + 写入 Qdrant
              │
              ▼
             END

扩展指南：
  - 添加新节点：graph.add_node("my_node", my_node_fn)
  - 添加顺序边：graph.add_edge("existing_node", "my_node")
  - 添加条件边：graph.add_conditional_edges("my_node", my_condition_fn)
  - 在 call_model 前插入节点（如 web 检索前置）：
        graph.add_edge("retrieve_context", "my_preprocess")
        graph.add_edge("my_preprocess", "call_model")
        （移除原来 retrieve_context → call_model 边）
"""
import logging
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from config import CHAT_MODEL, ROUTER_ENABLED
from graph.edges import should_continue
from graph.nodes import (
    compress_memory,
    make_call_model,
    make_retrieve_context,
    route_model,
    save_response, make_call_model_after_tool,
)
from graph.state import GraphState

logger = logging.getLogger("graph.agent")

# 按模型名缓存已编译的图，首次使用某模型时自动编译
_graph_cache: dict[str, Any] = {}
_tools: list[BaseTool] = []


def build_graph(tools: list[BaseTool], model: str = CHAT_MODEL) -> Any:
    tool_names = [t.name for t in tools]

    retrieve_fn = make_retrieve_context(tool_names)
    call_model_fn = make_call_model(tools)  # 用于普通回答
    call_model_tool_fn = make_call_model_after_tool(tools)

    # 注意：call_model_tool_fn 需要能覆盖 state.model，或者内部强制使用通用模型

    graph = StateGraph(GraphState)
    graph.add_node("retrieve_context", retrieve_fn)
    graph.add_node("call_model", call_model_fn)
    graph.add_node("call_model_after_tool", call_model_tool_fn)  # 新增节点
    graph.add_node("save_response", save_response)
    graph.add_node("compress_memory", compress_memory)

    if tools:
        tool_node = ToolNode(tools)
        graph.add_node("tools", tool_node)
        graph.add_conditional_edges(
            "call_model",
            should_continue,
            {"tools": "tools", "save_response": "save_response"},
        )
        # 工具执行后，改为调用 call_model_after_tool 节点
        graph.add_edge("tools", "call_model_after_tool")

        # 工具后节点也需要条件边
        graph.add_conditional_edges(
            "call_model_after_tool",
            should_continue,
            {"tools": "tools", "save_response": "save_response"},
        )
    else:
        graph.add_edge("call_model", "save_response")

    if ROUTER_ENABLED:
        graph.add_node("route_model", route_model)
        graph.add_edge(START, "route_model")
        graph.add_edge("route_model", "retrieve_context")
    else:
        graph.add_edge(START, "retrieve_context")

    graph.add_edge("retrieve_context", "call_model")
    graph.add_edge("save_response", "compress_memory")
    graph.add_edge("compress_memory", END)

    return graph.compile()


def init(tools: list[BaseTool], model: str = CHAT_MODEL) -> None:
    """应用启动时调用，编译并缓存图（路由模式下只需一张图）。"""
    global _tools
    _tools = tools
    _graph_cache["default"] = build_graph(tools, model)


def get_graph(model: str = CHAT_MODEL) -> Any:
    """返回已编译的图。路由模式下所有请求共用同一张图，model 由路由节点动态写入 state。"""
    if "default" not in _graph_cache:
        raise RuntimeError("Agent 图未初始化，请先调用 graph.agent.init(tools)")
    return _graph_cache["default"]
