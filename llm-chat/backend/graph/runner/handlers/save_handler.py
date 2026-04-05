"""
SaveResponseEndHandler：保存响应完成事件处理器

通知前端进入 saving 状态，防止保存期间静默导致前端误判断流结束。
"""
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class SaveResponseEndHandler(EventHandler):
    """save_response 完成：推送 saving 状态通知。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "save_response"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield sse({"status": "saving"})
