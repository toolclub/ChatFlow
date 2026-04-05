"""
ReflectorEndHandler：反思评估事件处理器

推送步骤状态更新和反思结论，供前端实时展示计划执行进度。
"""
from typing import AsyncGenerator

from graph.event_types import ReflectorEndEvent
from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class ReflectorEndHandler(EventHandler):
    """反思节点结束：推送步骤状态更新和反思内容。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "reflector" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = ReflectorEndEvent.from_event(event)
        if ev is None:
            return
        # 计划状态更新（步骤完成/推进）
        if ev.plan:
            yield sse({"plan_generated": {"steps": list(ev.plan)}})
        # 反思结论
        if ev.reflection or ev.reflector_decision:
            yield sse({
                "reflection": {
                    "content":  ev.reflection,
                    "decision": ev.reflector_decision,
                }
            })
