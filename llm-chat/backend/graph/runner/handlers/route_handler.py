"""
RouteStartHandler / RouteEndHandler：路由事件处理器

RouteStartHandler: 路由开始时通知前端进入 routing 状态
RouteEndHandler:   路由结束时推送选定模型和意图，更新 ctx.active_model
"""
from typing import AsyncGenerator

from config import ROUTE_MODEL_MAP
from graph.event_types import RouteEndEvent
from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse

# 模型名 → 意图标签的反向映射（用于 RouteEndHandler 推送）
_MODEL_TO_INTENT: dict[str, str] = {v: k for k, v in ROUTE_MODEL_MAP.items()}


class RouteStartHandler(EventHandler):
    """路由节点启动：通知前端进入 routing 状态。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield sse({"status": "routing"})


class RouteEndHandler(EventHandler):
    """路由节点结束：推送路由结果并更新会话激活模型。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = RouteEndEvent.from_event(event)
        if ev is None:
            return
        # 更新会话激活模型（后续 LLM 状态事件使用此模型名）
        ctx.active_model = ev.answer_model or ctx.active_model
        intent = ev.route or _MODEL_TO_INTENT.get(ctx.active_model, "chat")
        yield sse({"route": {"model": ctx.active_model, "intent": intent}})
