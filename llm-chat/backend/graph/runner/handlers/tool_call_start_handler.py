"""
ToolCallStartHandler：工具调用参数生成开始事件处理器

当 LLM 流式生成 tool_call 参数时（如生成 20KB HTML），第一个 delta 到达时立即通知前端。
前端据此提前显示终端 loading 状态，而非等待参数完全生成后才弹出终端（可能需要 30-60 秒）。

SSE 格式：
  data: {"tool_call_start": {"name": "sandbox_write"}}

前端收到后在消息中创建一个 pending 状态的 tool call 记录，sandbox 终端立即显示 loading。
"""
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class ToolCallStartHandler(EventHandler):
    """工具调用参数开始生成：让前端提前展示终端 loading。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_custom_event" and event_name == "tool_call_start"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        data = event.get("data", {})
        if not isinstance(data, dict):
            return
        name = data.get("name", "")
        if name:
            yield sse({"tool_call_start": {"name": name}})
