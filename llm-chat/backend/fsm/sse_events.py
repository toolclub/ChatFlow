"""
SSE 事件类型注册表 — 单一真相源

所有 SSE 事件类型在此注册，替代散落各处的硬编码字符串。
按优先级排序，避免多 key 共存时误判。

使用方式：
    from statemachine.sse_events import SSEEventType, detect_sse_event_type
    event_type = detect_sse_event_type({"tool_result": {...}, "content": "x"})
    # → SSEEventType.TOOL_RESULT（优先级高于 CONTENT）
"""
from __future__ import annotations

from enum import Enum


class SSEEventType(str, Enum):
    """SSE 事件类型，继承 str 使 JSON 序列化和 DB 存储无需转换。"""

    # ── 会话控制（最高优先级） ──
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"

    # ── 澄清 ──
    CLARIFICATION = "clarification"

    # ── 工具调用 ──
    TOOL_RESULT = "tool_result"
    TOOL_CALL = "tool_call"
    TOOL_CALL_START = "tool_call_start"
    SEARCH_ITEM = "search_item"
    SANDBOX_OUTPUT = "sandbox_output"

    # ── 文件产物 ──
    FILE_ARTIFACT = "file_artifact"

    # ── 认知/计划 ──
    PLAN_GENERATED = "plan_generated"
    REFLECTION = "reflection"

    # ── 状态/路由 ──
    STATUS = "status"
    ROUTE = "route"

    # ── 恢复上下文 ──
    RESUME_CONTEXT = "resume_context"

    # ── 流式内容（较低优先级） ──
    THINKING = "thinking"
    CONTENT = "content"

    # ── 心跳 ──
    PING = "ping"

    # ── 未知 ──
    UNKNOWN = "unknown"


# 检测优先级顺序（控制事件 > 工具 > 认知 > 内容 > 心跳）
_PRIORITY_ORDER: list[SSEEventType] = [
    SSEEventType.DONE,
    SSEEventType.STOPPED,
    SSEEventType.ERROR,
    SSEEventType.CLARIFICATION,
    SSEEventType.TOOL_RESULT,
    SSEEventType.TOOL_CALL,
    SSEEventType.TOOL_CALL_START,
    SSEEventType.SEARCH_ITEM,
    SSEEventType.SANDBOX_OUTPUT,
    SSEEventType.FILE_ARTIFACT,
    SSEEventType.PLAN_GENERATED,
    SSEEventType.REFLECTION,
    SSEEventType.STATUS,
    SSEEventType.ROUTE,
    SSEEventType.RESUME_CONTEXT,
    SSEEventType.THINKING,
    SSEEventType.CONTENT,
    SSEEventType.PING,
]


def detect_sse_event_type(data: dict) -> SSEEventType:
    """
    从 SSE payload dict 中检测事件类型。

    按优先级匹配，控制事件 > 工具 > 内容，
    避免多 key 共存时误判。
    """
    for etype in _PRIORITY_ORDER:
        if etype.value in data:
            return etype
    return SSEEventType.UNKNOWN
