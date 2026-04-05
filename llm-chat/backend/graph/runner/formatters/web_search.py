"""
WebSearchFormatter：网页搜索结果格式化器

将 web_search 工具返回的 JSON 列表格式化为逐条 search_item SSE 事件，
前端可实时追加显示每条搜索结果。
"""
import json
from typing import AsyncGenerator

from graph.runner.formatters.base import ToolResultFormatter
from graph.runner.utils import sse


class WebSearchFormatter(ToolResultFormatter):
    """
    网页搜索结果格式化器。

    输入：JSON 字符串，格式 [{"url": "...", "title": "...", ...}, ...]
    输出：每条结果一个 search_item 事件 + 最终 tool_result 事件
    """

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        """逐条推送搜索结果，每条一个 search_item SSE 事件。"""
        try:
            results = json.loads(raw)
            for item in results:
                url = item.get("url", "")
                yield sse({
                    "search_item": {
                        "url":    url,
                        "title":  item.get("title", ""),
                        "status": "done" if url else "fail",
                    }
                })
        except Exception:
            # JSON 解析失败时静默跳过，仍发送 tool_result 信号
            pass

        # 搜索完成信号
        yield sse({"tool_result": {"name": name}})
