"""quant.data — 公告 / 备案 / 事件类数据源（区别于 quant.providers 的行情数据源）。

分工：
    quant.providers.*   行情（K 线 / 快照 / 指数成分）—— 智能选股与预测共用，只读复用，别改
    quant.data.*        公告、定增、减持、负面事件、SEC 备案 —— 「股票预测 v2」新增

两个消费者：
    1. 预测图（编排层直调，文档 §4.1「不上 ToolNode」）
         from quant.data import announcement
         events = await announcement.fetch_negative_events("002594.SZ", months=24)
    2. 聊天工具（LLM function-call）
         tools/builtin/finance/stock_news.py

统一错误契约：
    数据源挂了 → 抛 AnnouncementDataError / SecEdgarError（两者都有 .source / .message）
    查到 0 条   → 返回 []（**绝不**把「数据源挂了」伪装成「这只票很干净」）
    调用方统一写：
        from quant.data import DATA_SOURCE_ERRORS
        try:
            items = await announcement.fetch_negative_events(sym, months=24)
        except DATA_SOURCE_ERRORS as exc:
            mark_dimension_failed(exc.message)      # 维度级降级（文档开放问题 #4）
"""
from __future__ import annotations

from quant.data.announcement import (
    CONF_HARD,
    CONF_INFERRED,
    EVENT_NEGATIVE,
    EVENT_NEUTRAL,
    EVENT_PLACEMENT,
    EVENT_REDUCTION,
    AnnouncementDataError,
    classify_notice,
    fetch_negative_events,
    fetch_placements,
    fetch_lockup_buyback,
    fetch_reductions,
    fetch_research_reports,
    fetch_stock_news,
    fetch_stock_notices,
    strip_highlight,
    title_hash,
)
from quant.data.sec_edgar import (
    SecEdgarError,
    fetch_sec_events,
    resolve_cik,
)

# 调用方一把捕获：`except DATA_SOURCE_ERRORS as exc: ...`（exc.source / exc.message 可用）
DATA_SOURCE_ERRORS: tuple[type[Exception], ...] = (AnnouncementDataError, SecEdgarError)

__all__ = [
    "AnnouncementDataError",
    "SecEdgarError",
    "DATA_SOURCE_ERRORS",
    "fetch_stock_notices",
    "fetch_negative_events",
    "fetch_placements",
    "fetch_lockup_buyback",
    "fetch_reductions",
    "fetch_stock_news",
    "fetch_research_reports",
    "fetch_sec_events",
    "resolve_cik",
    "classify_notice",
    "strip_highlight",
    "title_hash",
    "EVENT_NEGATIVE",
    "EVENT_PLACEMENT",
    "EVENT_REDUCTION",
    "EVENT_NEUTRAL",
    "CONF_HARD",
    "CONF_INFERRED",
]
