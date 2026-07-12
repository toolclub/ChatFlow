"""行情预检节点 — 图的第一个节点，秒级判定「这只票到底拉不拉得到行情」。

为什么需要它：
  原图顺序 START → news_search → compute → analyze，把那句 2 秒就能得出的行情判死
  排在了 11~16 分钟的 12 维资讯搜索之后 —— 用户等十几分钟，最后只收到一句误导性的
  「历史行情为空，无法预测」。预检把判死提到最前面：失败立刻短路（不跑资讯搜索），
  并给出诚实的原因（限流 / 网络不通 / 标的不存在）。

合规性（spec.md 铁律）：
  - #3 事件类型用 SSEEventType.*.value，不硬编码字符串字面量
  - #4 DB 操作全 async
  - #6 预检不做状态机转换：status 保持 queued，只发 PREDICTION_PROGRESS 事件
  - #9 异常不吞：MarketDataError 降级置 state.error/error_kind；未知异常先 logger.warning 再降级

终态（failed）由编排层 graph/prediction_agent.py 依据 state.error 统一落库 + 发 ERROR 事件，
节点内不写终态，避免两处写状态互相打架。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.callbacks.manager import adispatch_custom_event

from db.prediction_store import update_prediction_progress
from fsm.sse_events import SSEEventType
from graph.nodes.base import BaseNode
from graph.prediction.market_data import (
    KIND_BAD_SYMBOL,
    KIND_NO_DATA,
    MarketDataError,
    preflight,
)

if TYPE_CHECKING:
    from graph.prediction_agent import PredictState

logger = logging.getLogger("graph.nodes.prediction.preflight")

# 阶段标识（前后端共用，与 DB progress.phase 同值）
PHASE_PREFLIGHT = "preflight"
PHASE_NEWS_SEARCHING = "news_searching"
PHASE_INFERRING = "inferring"


async def emit_progress(
    snapshot_id: str,
    phase: str,
    detail: str,
    *,
    label: str = "",
    index: int | None = None,
    total: int | None = None,
    status: str = "",
    persist: bool = True,
) -> None:
    """派发 PREDICTION_PROGRESS 事件 + 覆盖写 DB progress 字段。

    两条路径各有分工：
      - SSE 事件：把长时间静默的空档填满（实测单次运行有 12+ 个 >15s 的零字节空档，
        最长 198s），让用户看到"正在搜索：地缘政治 (3/12)"而不是以为卡死。
      - DB progress：刷新页面时前端直接读到"当前在哪一步"，不必重放 4000+ 条 thinking 事件。

    进度是纯展示信息，写失败绝不能阻塞图执行：update_prediction_progress 内部已保证不抛，
    这里再给 dispatch 兜一层 try（铁律 #9：至少 logger.warning）。

    供本包内所有预测节点复用（preflight / compute / analyze / news_search）。
    """
    payload: dict = {"phase": phase, "detail": detail}
    if label:
        payload["label"] = label
    if index is not None:
        payload["index"] = index
    if total is not None:
        payload["total"] = total
    if status:
        payload["status"] = status

    # 铁律 #3：事件类型取自 SSEEventType 枚举
    try:
        await adispatch_custom_event(
            "prediction_event",
            {"type": SSEEventType.PREDICTION_PROGRESS.value, **payload},
        )
    except Exception as exc:
        logger.warning("dispatch prediction_progress(phase=%s) 失败: %s", phase, exc)

    if persist and snapshot_id:
        await update_prediction_progress(snapshot_id, payload)


class PreflightNode(BaseNode):
    """行情预检：只拉最近 ~10 个交易日，验证标的可取数（缓存命中时亚秒级）。"""

    @property
    def name(self) -> str:
        return "predict_preflight"

    async def execute(self, state: "PredictState") -> dict:
        snapshot_id = state.get("snapshot_id", "")
        symbol = (state.get("symbol") or "").strip()
        market = state.get("market", "cn_a")

        await emit_progress(
            snapshot_id,
            PHASE_PREFLIGHT,
            f"正在校验 {symbol or '标的'} 的行情数据…",
            label="行情预检",
            index=0,
            total=1,
            status="queued",
        )

        if not symbol:
            msg = "未指定标的代码，无法预测"
            logger.warning("predict_preflight 缺少 symbol snap=%s", snapshot_id)
            await emit_progress(
                snapshot_id, PHASE_PREFLIGHT, f"行情校验失败：{msg}",
                label="行情预检", index=1, total=1, status="queued",
            )
            return {
                **state,
                "error": msg,
                "error_kind": KIND_BAD_SYMBOL,
                "status": "queued",
            }

        try:
            result = await preflight(symbol, market)
        except MarketDataError as exc:
            # 诚实的失败原因（rate_limited / network / no_data / bad_symbol）。
            # 编排层据 state.error 短路到 END —— 不再白跑 10 分钟资讯搜索。
            logger.warning(
                "predict_preflight 失败 snap=%s symbol=%s market=%s kind=%s: %s",
                snapshot_id, symbol, market, exc.kind, exc.message,
            )
            await emit_progress(
                snapshot_id, PHASE_PREFLIGHT, f"行情校验失败：{exc.message}",
                label="行情预检", index=1, total=1, status="queued",
            )
            return {
                **state,
                "error": exc.message,
                "error_kind": exc.kind,
                "status": "queued",
            }
        except Exception as exc:
            # market_data.preflight 已把未知异常包成 MarketDataError；此处纯兜底不让图崩（铁律 #9）
            logger.warning(
                "predict_preflight 未预期异常 snap=%s symbol=%s: %s",
                snapshot_id, symbol, exc, exc_info=True,
            )
            msg = f"行情预检异常：{exc}"
            await emit_progress(
                snapshot_id, PHASE_PREFLIGHT, f"行情校验失败：{msg}",
                label="行情预检", index=1, total=1, status="queued",
            )
            return {
                **state,
                "error": msg,
                "error_kind": KIND_NO_DATA,
                "status": "queued",
            }

        bars = int(result.get("bars") or 0)
        # 归一化后的代码（"000001" → "000001.SZ"，美股统一大写）回写 state，
        # 让下游 news_search / compute 用同一个代码，避免各自再拿用户原始输入去拉。
        normalized = str(result.get("symbol") or symbol)

        logger.info(
            "predict_preflight 通过 snap=%s symbol=%s market=%s bars=%d last_close=%s",
            snapshot_id, normalized, market, bars, result.get("last_close"),
        )
        await emit_progress(
            snapshot_id,
            PHASE_PREFLIGHT,
            f"行情数据校验通过 · {bars} 根K线",
            label="行情预检",
            index=1,
            total=1,
            status="queued",
        )

        return {
            **state,
            "symbol": normalized,
            "preflight": result,
            "status": "queued",
        }
