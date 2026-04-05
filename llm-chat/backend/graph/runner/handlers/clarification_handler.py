"""
ClarificationHandler：澄清问询事件处理器

当 save_response 节点检测到模型在向用户问询时，
通过 adispatch_custom_event("clarification_needed", data) 派发此事件。

handler 将结构化问题 JSON 包装为 SSE 推送给前端，
前端渲染为可交互的澄清卡片。
"""
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class ClarificationHandler(EventHandler):
    """
    监听 on_custom_event + clarification_needed，推送澄清卡片数据到前端。

    事件数据格式（来自 save_response_node._generate_clarification_data）：
    {
        "question": "需要澄清的核心问题",
        "items": [
            {"id": "...", "type": "single_choice|multi_choice|text", "label": "...", ...}
        ]
    }
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_custom_event" and event_name == "clarification_needed"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        data = event.get("data", {})
        if not isinstance(data, dict):
            return

        question = data.get("question", "")
        items    = data.get("items", [])

        if not question and not items:
            return

        yield sse({"clarification": {"question": question, "items": items}})
