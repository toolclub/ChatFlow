"""
向后兼容桥接 — 转发到 statemachine/ 模块。

旧代码通过 `from db.state_machine import ...` 导入，
此文件将所有公开 API 转发到新的 statemachine/ 包。
"""
from fsm.sse_events import SSEEventType, detect_sse_event_type
from fsm.conversation import ConversationSM
from fsm.tool_execution import ToolExecutionSM


def validate_conv_transition(current: str, target_status: str) -> bool:
    """校验对话状态转换是否合法（委托给 ConversationSM）。"""
    try:
        sm = ConversationSM.from_db_status(current)
        result = sm.send_event(target_status)
        return result == target_status
    except Exception:
        return False


def validate_tool_transition(current: str, target_status: str) -> bool:
    """校验工具执行状态转换是否合法（委托给 ToolExecutionSM）。"""
    if current != "running":
        return False
    try:
        sm = ToolExecutionSM()
        sm.send_event(target_status)
        return True
    except Exception:
        return False


# 向后兼容的枚举（直接用字符串常量，不再自定义 Enum）
class ConversationStatus:
    ACTIVE = "active"
    STREAMING = "streaming"
    COMPLETED = "completed"
    ERROR = "error"

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.value == other
        return NotImplemented


class ToolExecutionStatus:
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    TIMEOUT = "timeout"

    def __init__(self, value: str) -> None:
        if value not in ("running", "done", "error", "timeout"):
            raise ValueError(f"Invalid tool execution status: {value}")
        self.value = value


__all__ = [
    "ConversationSM",
    "ToolExecutionSM",
    "SSEEventType",
    "detect_sse_event_type",
    "validate_conv_transition",
    "validate_tool_transition",
    "ConversationStatus",
    "ToolExecutionStatus",
]
