"""
CompressEndHandler：记忆压缩完成事件处理器

更新 ctx.compressed 状态，供 stream_response 在 done 事件中告知前端。
不产生 SSE 输出（压缩结果通过 done 事件的 compressed 字段传递）。
"""
from typing import AsyncGenerator

from graph.event_types import CompressEndEvent
from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler


class CompressEndHandler(EventHandler):
    """记忆压缩完成：更新 ctx.compressed，不产生 SSE 输出。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "compress_memory"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = CompressEndEvent.from_event(event)
        ctx.compressed = ev.compressed
        # 声明为 AsyncGenerator 但不 yield（只更新 ctx 状态）
        return
        yield  # noqa: unreachable - 保持 AsyncGenerator 类型
