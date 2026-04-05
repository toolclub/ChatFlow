"""
graph.nodes 包：LangGraph 图节点集合

所有节点类统一从此处导出，供 graph.agent 使用。

使用方式（agent.py 中）：
    from graph.nodes import (
        SemanticCacheNode, VisionNode, RouteNode,
        RetrieveContextNode, PlannerNode, CallModelNode,
        CallModelAfterToolNode, ReflectorNode,
        SaveResponseNode, CompressNode,
    )

    # 实例化时注入依赖
    call_model_node = CallModelNode(tools)
    retrieve_node   = RetrieveContextNode(tool_names)

    # 注册时使用 .execute 方法
    graph.add_node("call_model", call_model_node.execute)
"""
from graph.nodes.cache_node import SemanticCacheNode
from graph.nodes.call_model_after_tool_node import CallModelAfterToolNode
from graph.nodes.call_model_node import CallModelNode
from graph.nodes.compress_node import CompressNode
from graph.nodes.planner_node import PlannerNode
from graph.nodes.reflector_node import ReflectorNode
from graph.nodes.retrieve_context_node import RetrieveContextNode
from graph.nodes.route_node import RouteNode
from graph.nodes.save_response_node import SaveResponseNode
from graph.nodes.vision_node import VisionNode

__all__ = [
    "SemanticCacheNode",
    "VisionNode",
    "RouteNode",
    "RetrieveContextNode",
    "PlannerNode",
    "CallModelNode",
    "CallModelAfterToolNode",
    "ReflectorNode",
    "SaveResponseNode",
    "CompressNode",
]
