"""
ToolStartHandler / ToolEndHandler：工具调用事件处理器

ToolStartHandler: 工具调用开始时推送工具名和入参
ToolEndHandler:   工具调用完成时委托格式化策略推送结果
"""
from typing import AsyncGenerator

from graph.event_types import ToolEndEvent, ToolStartEvent
from graph.runner.context import StreamContext
from graph.runner.formatters import get_formatter
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class ToolStartHandler(EventHandler):
    """工具调用开始：向前端推送工具名和入参（供进度展示）。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_start"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = ToolStartEvent.from_event(event)
        from tools.skill import SkillRegistry
        display_mode = SkillRegistry.instance().get_display_mode(ev.name)
        yield sse({"tool_call": {"name": ev.name, "input": ev.input, "display_mode": display_mode}})


class ToolEndHandler(EventHandler):
    """工具调用完成：委托注册的格式化策略推送结果。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_end"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev        = ToolEndEvent.from_event(event)
        formatter = get_formatter(ev.name)
        async for chunk in formatter.format(ev.name, ev.raw_output):
            yield chunk
