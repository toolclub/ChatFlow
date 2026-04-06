"""
LangGraph Agent 图构建与全局单例管理

完整图结构（ROUTER_ENABLED=true）：
    START
      │
      ▼
    semantic_cache_check   ← 语义缓存检查（最前置节点）
      │
    cache_routing?
      ├── save_response     ← Cache HIT：跳过 LLM 全流程，直接保存
      │
      └── vision_node       ← Cache MISS：视觉理解（有图片时生成描述，无图片时透传）
            │
            ▼
          route_model       ← 路由决策（利用 vision_description 更精准选择模型）
            │
            ▼
          retrieve_context  ← 检索 RAG + 组装历史消息
            │
            ▼
          planner           ← 生成执行计划（search/search_code 路由触发）
            │
            ▼
          call_model        ← LLM 推理（当前步骤）
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
    semantic_cache_check → vision_node → (miss) → route_model → retrieve_context → planner →
    call_model → (无工具) → save_response → compress_memory → END
"""
import logging
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from config import CHAT_MODEL, ROUTER_ENABLED
from graph.edges import (
    cache_routing,
    reflector_routing,
    should_continue,
    should_continue_after_tool,
)
from graph.nodes import (
    CallModelAfterToolNode,
    CallModelNode,
    CompressNode,
    PlannerNode,
    ReflectorNode,
    RetrieveContextNode,
    RouteNode,
    SaveResponseNode,
    SemanticCacheNode,
    VisionNode,
)
from graph.state import GraphState

logger = logging.getLogger("graph.agent")

_graph_cache: dict[str, Any] = {}
_tools: list[BaseTool] = []


def build_graph(tools: list[BaseTool], model: str = CHAT_MODEL) -> Any:
    """
    构建并编译 LangGraph 图。

    所有节点通过 NodeClass().execute 注册，确保 LangGraph 接收的是可调用函数。
    节点依赖（tools / tool_names）通过构造函数注入，不依赖全局变量。
    """
    tool_names = [t.name for t in tools]

    # ── 实例化各节点（注入依赖） ──────────────────────────────────────────────
    cache_node             = SemanticCacheNode()
    vision_node            = VisionNode()
    route_node             = RouteNode()
    retrieve_node          = RetrieveContextNode(tool_names)
    planner_node           = PlannerNode()
    call_model_node        = CallModelNode(tools)
    call_model_tool_node   = CallModelAfterToolNode(tools)
    reflector_node         = ReflectorNode()
    save_response_node     = SaveResponseNode()
    compress_node          = CompressNode()

    graph = StateGraph(GraphState) # type: ignore[arg-type]

    # ── 注册节点（第二个参数为可调用函数） ──────────────────── ──────────────
    graph.add_node("semantic_cache_check",   cache_node.execute) # type: ignore[arg-type]
    graph.add_node("vision_node",            vision_node.execute) # type: ignore[arg-type]
    graph.add_node("retrieve_context",       retrieve_node.execute) # type: ignore[arg-type]
    graph.add_node("planner",                planner_node.execute) # type: ignore[arg-type]
    graph.add_node("call_model",             call_model_node.execute) # type: ignore[arg-type]
    graph.add_node("call_model_after_tool",  call_model_tool_node.execute) # type: ignore[arg-type]
    graph.add_node("reflector",              reflector_node.execute) # type: ignore[arg-type]
    graph.add_node("save_response",          save_response_node.execute) # type: ignore[arg-type]
    graph.add_node("compress_memory",        compress_node.execute) # type: ignore[arg-type]

    # ── 工具节点（LangGraph 内置，依赖 LangChain 消息格式） ──────────────────
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
        # 无工具模式：call_model 直接到 reflector 或 save_response
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
    graph.add_edge("retrieve_context",  "planner")
    graph.add_edge("planner",           "call_model")
    graph.add_edge("save_response",     "compress_memory")
    graph.add_edge("compress_memory",   END)

    # ── 入口：semantic_cache_check 始终是第一个节点 ──────────────────────────
    graph.add_edge(START, "semantic_cache_check")

    # ── cache_routing：命中 → save_response；未命中 → vision_node ────────────
    if ROUTER_ENABLED:
        graph.add_node("route_model", route_node.execute) # type: ignore[arg-type]
        graph.add_conditional_edges(
            "semantic_cache_check",
            cache_routing,
            {"save_response": "save_response", "after_cache": "vision_node"},
        )
        # vision_node → route_model → retrieve_context（带路由的标准路径）
        graph.add_edge("vision_node",   "route_model")
        graph.add_edge("route_model",   "retrieve_context")
    else:
        # 无路由模式：vision_node → retrieve_context（直接跳过路由）
        graph.add_conditional_edges(
            "semantic_cache_check",
            cache_routing,
            {"save_response": "save_response", "after_cache": "vision_node"},
        )
        graph.add_edge("vision_node", "retrieve_context")

    return graph.compile()


def build_simple_graph(tools: list[BaseTool], model: str = CHAT_MODEL) -> Any:
    """
    构建简单对话图（无 planner / reflector / route_model LLM 调用）。

    适用场景：直接问答、不需要多步规划的普通对话。

    流程：
        START → semantic_cache_check
          ├── HIT  → save_response
          └── MISS → vision_node → retrieve_context → call_model
                       ├── tools → call_model_after_tool → save_response
                       └── done → save_response → compress_memory → END

    与完整图的差异：
      - 没有 route_model（LLM 路由调用），初始 state 强制 route="chat"
      - 没有 planner（无多步计划）
      - 没有 reflector（无自我评估循环）
      - should_continue 返回 "reflector" 时映射到 "save_response"，直接结束
    """
    tool_names = [t.name for t in tools]

    cache_node           = SemanticCacheNode()
    vision_node          = VisionNode()
    retrieve_node        = RetrieveContextNode(tool_names)
    call_model_node      = CallModelNode(tools)
    call_model_tool_node = CallModelAfterToolNode(tools)
    save_response_node   = SaveResponseNode()
    compress_node        = CompressNode()

    graph = StateGraph(GraphState)  # type: ignore[arg-type]

    graph.add_node("semantic_cache_check",  cache_node.execute)           # type: ignore[arg-type]
    graph.add_node("vision_node",           vision_node.execute)          # type: ignore[arg-type]
    graph.add_node("retrieve_context",      retrieve_node.execute)        # type: ignore[arg-type]
    graph.add_node("call_model",            call_model_node.execute)      # type: ignore[arg-type]
    graph.add_node("call_model_after_tool", call_model_tool_node.execute) # type: ignore[arg-type]
    graph.add_node("save_response",         save_response_node.execute)   # type: ignore[arg-type]
    graph.add_node("compress_memory",       compress_node.execute)        # type: ignore[arg-type]

    if tools:
        tool_node = ToolNode(tools)
        graph.add_node("tools", tool_node)
        # "reflector" 映射到 "save_response"：简单图跳过自我评估，直接存储
        graph.add_conditional_edges(
            "call_model",
            should_continue,
            {"tools": "tools", "reflector": "save_response", "save_response": "save_response"},
        )
        graph.add_edge("tools", "call_model_after_tool")
        graph.add_conditional_edges(
            "call_model_after_tool",
            should_continue_after_tool,
            {"tools": "tools", "reflector": "save_response", "save_response": "save_response"},
        )
    else:
        graph.add_conditional_edges(
            "call_model",
            should_continue,
            {"reflector": "save_response", "save_response": "save_response"},
        )

    graph.add_edge(START, "semantic_cache_check")
    graph.add_conditional_edges(
        "semantic_cache_check",
        cache_routing,
        {"save_response": "save_response", "after_cache": "vision_node"},
    )
    graph.add_edge("vision_node",      "retrieve_context")
    graph.add_edge("retrieve_context", "call_model")
    graph.add_edge("save_response",    "compress_memory")
    graph.add_edge("compress_memory",  END)

    return graph.compile()


def init(tools: list[BaseTool], model: str = CHAT_MODEL) -> None:
    """应用启动时调用，编译并缓存两张图（完整 Agent 图 + 简单对话图）。"""
    global _tools
    _tools = tools
    _graph_cache["default"] = build_graph(tools, model)
    _graph_cache["simple"]  = build_simple_graph(tools, model)
    logger.info(
        "Agent 图初始化完成 | tools=%d | router_enabled=%s | 简单图=已就绪",
        len(tools), ROUTER_ENABLED,
    )


def get_graph(model: str = CHAT_MODEL) -> Any:
    """返回完整 Agent 图（含 planner / reflector / router）。"""
    if "default" not in _graph_cache:
        raise RuntimeError("Agent 图未初始化，请先调用 graph.agent.init(tools)")
    return _graph_cache["default"]


def get_simple_graph(model: str = CHAT_MODEL) -> Any:
    """返回简单对话图（无 planner / reflector / router）。"""
    if "simple" not in _graph_cache:
        raise RuntimeError("Simple 图未初始化，请先调用 graph.agent.init(tools)")
    return _graph_cache["simple"]
