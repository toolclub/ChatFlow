"""股票预测 LangGraph 节点（独立于主对话 graph）。

编排：PreflightNode → NewsSearchNode → ComputeNode → AnalyzeNode（由 graph/prediction_agent.py 装配，
带条件边：任一节点置了 state.error 就短路到 END）。PreflightNode 排在最前面，是为了让「这只票拉不到
行情」这种秒级就能判死的失败立刻返回，而不是让用户等完 11~16 分钟的资讯搜索才看到红错。

节点内通过 BaseNode.emit_thinking 推送 reasoning/content（spec §模型思考流程铁律 #7），
并通过 adispatch_custom_event("prediction_event", ...) 派发 NEWS_ITEM/FORECAST/INFLECTION/
PREDICTION_PROGRESS 事件（铁律 #3：用 SSEEventType 枚举值，不硬编码字符串）。
阶段进度统一走 emit_progress()：一边发 SSE 填满静默空档，一边覆盖写 DB progress 字段供刷新恢复。
"""
from graph.nodes.prediction.preflight_node import (
    PHASE_INFERRING,
    PHASE_NEWS_SEARCHING,
    PHASE_PREFLIGHT,
    PreflightNode,
    emit_progress,
)
from graph.nodes.prediction.news_search_node import NewsSearchNode
from graph.nodes.prediction.compute_node import ComputeNode
from graph.nodes.prediction.analyze_node import AnalyzeNode

__all__ = [
    "PreflightNode",
    "NewsSearchNode",
    "ComputeNode",
    "AnalyzeNode",
    "emit_progress",
    "PHASE_PREFLIGHT",
    "PHASE_NEWS_SEARCHING",
    "PHASE_INFERRING",
]
