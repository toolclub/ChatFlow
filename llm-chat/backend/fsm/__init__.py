"""
状态机模块 — 基于 python-statemachine 框架

提供对话、工具执行两个领域的状态机，以及 SSE 事件类型注册表。

使用方式：
    from fsm import ConversationSM, ToolExecutionSM, SSEEventType
"""
from fsm.conversation import ConversationSM
from fsm.tool_execution import ToolExecutionSM
from fsm.sse_events import SSEEventType, detect_sse_event_type

__all__ = [
    "ConversationSM",
    "ToolExecutionSM",
    "SSEEventType",
    "detect_sse_event_type",
]
