"""
formatters 包：工具结果格式化器注册表

添加新的格式化器步骤：
  1. 在本目录新建 xxx.py，实现 ToolResultFormatter 子类
  2. 在下方 REGISTRY 中注册工具名 → 格式化器实例的映射
"""
from graph.runner.formatters.base import ToolResultFormatter
from graph.runner.formatters.fetch_webpage import FetchWebpageFormatter
from graph.runner.formatters.generic import GenericToolFormatter
from graph.runner.formatters.web_search import WebSearchFormatter

# ── 工具名 → 格式化器实例映射 ─────────────────────────────────────────────────
# 未在此注册的工具自动使用 DEFAULT_FORMATTER
REGISTRY: dict[str, ToolResultFormatter] = {
    "web_search":    WebSearchFormatter(),
    "fetch_webpage": FetchWebpageFormatter(),
}

DEFAULT_FORMATTER: ToolResultFormatter = GenericToolFormatter()


def get_formatter(tool_name: str) -> ToolResultFormatter:
    """根据工具名返回对应格式化器，未注册工具返回默认格式化器。"""
    return REGISTRY.get(tool_name, DEFAULT_FORMATTER)


__all__ = [
    "ToolResultFormatter",
    "WebSearchFormatter",
    "FetchWebpageFormatter",
    "GenericToolFormatter",
    "REGISTRY",
    "DEFAULT_FORMATTER",
    "get_formatter",
]
