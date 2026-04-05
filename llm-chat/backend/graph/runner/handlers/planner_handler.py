"""
PlannerStartHandler / PlannerEndHandler：规划事件处理器

PlannerStartHandler: 规划节点启动时通知前端进入 planning 状态
PlannerEndHandler:   规划节点结束时推送生成的计划步骤
"""
from typing import AsyncGenerator

from graph.event_types import PlannerEndEvent
from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class PlannerStartHandler(EventHandler):
    """规划节点启动：通知前端进入 planning 状态。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "planner" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield sse({"status": "planning"})


class PlannerEndHandler(EventHandler):
    """
    规划节点结束：将生成的计划推送给前端。
    同时记录步骤数到 ctx，用于后续 step_update 事件的去重判断。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "planner" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = PlannerEndEvent.from_event(event)
        if ev is None or not ev.plan:
            return
        ctx.last_plan_step_count = len(ev.plan)
        yield sse({"plan_generated": {"steps": list(ev.plan)}})
