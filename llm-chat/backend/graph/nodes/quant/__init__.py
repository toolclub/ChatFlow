"""量化模块 LangGraph 节点（独立于主对话 graph）。

目前仅含 AnalyzeNode（snapshot LLM 洞察），由 graph/quant_agent.py 编排。
"""
from graph.nodes.quant.analyze_node import AnalyzeNode

__all__ = ["AnalyzeNode"]
