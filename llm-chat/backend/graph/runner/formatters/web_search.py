"""
WebSearchFormatter：网页搜索结果格式化器

将 web_search 工具返回的 JSON 格式化为逐条 search_item SSE 事件，
前端可实时追加显示每条搜索结果。

支持两种结果结构：
  - 新（推荐）：{"answer": "...", "results": [{"url","title","snippet","chunks"}, ...]}
  - 旧（COMPAT）：[{"url","title","snippet"}, ...]
"""
import json
from typing import AsyncGenerator

from graph.runner.formatters.base import ToolResultFormatter
from graph.runner.utils import sse


class WebSearchFormatter(ToolResultFormatter):
    """逐条推送搜索结果，每条一个 search_item SSE 事件。"""

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        results = _extract_results(raw)
        for item in results:
            url = item.get("url", "")
            yield sse({
                "search_item": {
                    "url":    url,
                    "title":  item.get("title", ""),
                    "status": "done" if url else "fail",
                }
            })

        yield sse({"tool_result": {"name": name}})


def _extract_results(raw: str) -> list[dict]:
    """从 raw JSON 中取出 results 列表，兼容新旧两种结构。"""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        return data.get("results") or []
    if isinstance(data, list):
        return data  # COMPAT: 旧版工具返回纯 list
    return []
