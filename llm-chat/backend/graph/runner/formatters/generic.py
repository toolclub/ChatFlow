"""
GenericToolFormatter：通用工具结果格式化器

用于未注册专属格式化器的工具，将原始输出截断后推送。
作为 formatters 注册表的兜底策略。
"""
from typing import AsyncGenerator

from graph.runner.formatters.base import ToolResultFormatter
from graph.runner.utils import sse

# 通用工具输出最大展示长度
_MAX_OUTPUT_LEN = 1000


class GenericToolFormatter(ToolResultFormatter):
    """
    通用工具结果格式化器（兜底策略）。

    将原始输出截断后通过 tool_result 事件推送。
    未注册专属格式化器的工具都使用此格式化器。
    """

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        """推送截断后的工具输出。"""
        yield sse({"tool_result": {"name": name, "output": raw[:_MAX_OUTPUT_LEN]}})
