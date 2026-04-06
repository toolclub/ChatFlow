"""
视觉节点事件处理器

VisionStartHandler:  VisionNode 开始处理图片时派发 vision_analyze 事件，
                     转换为前端状态通知：{"status": "vision_analyze"}。

VisionStreamHandler: VisionNode 流式分析图片时派发 vision_token 事件，
                     转换为 {"thinking": delta} 推给前端，
                     显示在消息"思考过程"折叠块中，让用户看到图像分析实时过程。
"""
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse


class VisionStartHandler(EventHandler):
    """视觉分析开始：通知前端进入"图像解析中"状态。"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_custom_event" and event_name == "vision_analyze"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield sse({"status": "vision_analyze"})


class VisionStreamHandler(EventHandler):
    """
    视觉分析流式 token 处理器。

    将 VisionNode 的逐 token 输出（vision_token 事件）转发给前端，
    以 thinking 类型呈现，让用户实时看到 GLM-4.6V 的图像分析过程。

    前端 onThinking 回调将内容追加到 msg().thinking，
    与主推理模型（MiniMax 等）的 thinking 内容共用同一个折叠块，
    中间由 VisionNode 注入的 "---" 分隔符隔开。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_custom_event" and event_name == "vision_token"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        content = event.get("data", {}).get("content", "")
        if content:
            yield sse({"thinking": content})
