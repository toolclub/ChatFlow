"""
CacheHitEndHandler：语义缓存命中事件处理器

命中时推送 cache_hit 状态 + 完整答案（复用 content 格式，前端无需改动）。
"""
from typing import AsyncGenerator

from graph.event_types import CacheHitEndEvent
from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class CacheHitEndHandler(EventHandler):
    """处理 semantic_cache_check 节点结束事件：缓存命中时推送答案。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and "semantic_cache_check" in (event_name, node_name)
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = CacheHitEndEvent.from_event(event)
        if not ev.cache_hit:
            return
        # 推送缓存命中状态（含相似度）
        yield sse({"status": "cache_hit", "similarity": round(ev.cache_similarity, 4)})
        # 推送完整答案（前端通过 content 事件渲染）
        if ev.full_response:
            yield sse({"content": ev.full_response})
