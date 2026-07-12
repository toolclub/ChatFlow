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
    PAUSED = "paused"                        # 暂停确认（与 STOPPED 平级；但 paused 非终态，可 resume）
    ERROR = "error"

    # ── 澄清 ──
    CLARIFICATION = "clarification"

    # ── 工具调用 ──
    TOOL_RESULT = "tool_result"
    TOOL_CALL = "tool_call"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS = "tool_call_args"
    SEARCH_ITEM = "search_item"
    SANDBOX_OUTPUT = "sandbox_output"

    # ── 文件产物 ──
    FILE_ARTIFACT = "file_artifact"

    # ── 股票预测专用（铁律 #3：注册而非硬编码，修正 quant 把 "delta" 硬编码的违规） ──
    TOOL_TRACE = "tool_trace"                # 工具调用轨迹（"正在调用 stock_announcements(…)" / "返回 388 条，1.9s"）
    NEWS_ITEM = "news_item"                  # 资讯搜索结果流入（每维一条，含完整原文）
    DATA_ITEM = "data_item"                  # 结构化数据卡片（公告/定增/减持/SEC filing），与 NEWS_ITEM 平级
    FORMULA_STEP = "formula_step"            # 单个公式：formula + params + substituted + result + explain
    FORECAST_DAY = "forecast_day"            # 逐日预测蜡烛增量 {day, date, o, h, l, c, p10, p50, p90}
    FORECAST = "forecast"                     # 预测数值结果（方向/幅度/置信区间）
    INFLECTION = "inflection"                  # 拐点标注
    EVENT_MARKER = "event_marker"            # K 线上的公告事件打点 {date, event_type, title, score}
    PREDICTION_PROGRESS = "prediction_progress"  # 阶段进度（对应状态机转换）

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
    SSEEventType.PAUSED,
    SSEEventType.ERROR,
    SSEEventType.CLARIFICATION,
    SSEEventType.TOOL_RESULT,
    SSEEventType.TOOL_CALL,
    SSEEventType.TOOL_CALL_START,
    SSEEventType.TOOL_CALL_ARGS,
    SSEEventType.SEARCH_ITEM,
    SSEEventType.TOOL_TRACE,
    SSEEventType.NEWS_ITEM,
    SSEEventType.DATA_ITEM,
    SSEEventType.FORMULA_STEP,
    SSEEventType.FORECAST_DAY,
    SSEEventType.FORECAST,
    SSEEventType.INFLECTION,
    SSEEventType.EVENT_MARKER,
    SSEEventType.PREDICTION_PROGRESS,
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
