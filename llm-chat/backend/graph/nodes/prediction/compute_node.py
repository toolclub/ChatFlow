"""计算节点 — 拉取行情 + 调用 forecast 引擎（流式推导）+ 持久化预测结果。

行情拉取走 graph/prediction/market_data.py 的加固客户端（重试退避 + provider 兜底 +
诚实的错误分类），不再裸调 quant.data_adapter：那条路径在雅虎 429 限流时只会返回一个空
DataFrame，节点便报出误导性的「历史行情为空，无法预测」—— 用户完全分不清是限流、
是网络不通、还是代码写错了。现在错的是什么就说什么。

v2 改动（设计文档 §5.6 / §5.4 / §6）：
  - 把 filing_digest（数据型维度结果）喂给 engine.forecast → 事件分 F 进漂移/波动修正
  - on_step 回调：engine 边算边推 FORMULA_STEP（公式卡片）+ FORECAST_DAY（7 根蜡烛逐根长出）
    → SSE 派发 + **逐条增量落库**（刷新页面靠 DB 列重建，不靠重放几千条事件）
  - EVENT_MARKER：把 filing_digest 里的公告按日期打到 K 线上（只标有负面分的）
  - model_params 全参数落库：ARIMA/GARCH/事件分/GBM/MC/影线标定，供前端复现整条推导

合规性（spec.md 铁律）：
  - #1 状态从 DB 字段读（不推断文本）
  - #2 结构化结果走独立 JSONB 字段（forecast/model_params/formula_steps/...），不塞 content
  - #3 事件类型用 SSEEventType.*.value（FORMULA_STEP / FORECAST_DAY / EVENT_MARKER 亦然）
  - #5 DB/adapter 操作全 async
  - #7 状态变更先 PredictionSM.send_event，再 update_prediction_status
  - #9 异常不吞，行情失败 / forecast 失败均降级置 error + error_kind
  - 计算引擎 forecast() 本身是纯数学 + asyncio.to_thread，无 LLM/DB（见 engine.py）
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd
from langchain_core.callbacks.manager import adispatch_custom_event

from db.prediction_store import (
    append_prediction_forecast_candle,
    append_prediction_formula_step,
    update_prediction_candles,
    update_prediction_event_markers,
    update_prediction_forecast,
    update_prediction_formula_steps,
    update_prediction_historical_bars,
    update_prediction_model_params,
    update_prediction_status,
)
from fsm.prediction import PredictionSM
from fsm.sse_events import SSEEventType
from graph.nodes.base import BaseNode
from graph.nodes.prediction.preflight_node import PHASE_INFERRING, emit_progress
from graph.prediction.announcements import aggregate_event_score, to_event_markers
from graph.prediction.config import PREDICTION_BARS_LOOKBACK_DAYS, PREDICTION_HORIZON_DAYS
from graph.prediction.domain import bars_to_chart
from graph.prediction.engine import forecast
from graph.prediction.market_data import KIND_NO_DATA, MarketDataError, fetch_bars

if TYPE_CHECKING:
    from graph.prediction_agent import PredictState

logger = logging.getLogger("graph.nodes.prediction.compute")

# K 线上最多打多少个公告点（再多就糊成一片，且 JSONB 行会撑大）
EVENT_MARKER_LIMIT = 30


class ComputeNode(BaseNode):
    """行情拉取 + ARIMA/GARCH/事件分/蒙特卡洛预测节点（流式推导）。"""

    @property
    def name(self) -> str:
        return "predict_compute"

    async def execute(self, state: "PredictState") -> dict:
        snapshot_id = state.get("snapshot_id", "")
        symbol = state.get("symbol", "")
        market = state.get("market", "cn_a")
        horizon = int(state.get("horizon") or PREDICTION_HORIZON_DAYS)
        filing_digest = list(state.get("filing_digest") or [])

        # 上游已失败（预检 / 资讯搜索）→ 原样返回，不做任何状态转换和计算。
        # 正常情况下编排层的条件边已经短路到 END，这里只是双保险。
        if state.get("error"):
            return dict(state)

        # ── 铁律 #7：状态机先校验 news_searching → inferring，再写 DB ──────────
        # 续跑（resume_infer）时状态已是 inferring：再转一次是非法跳转（只会记 warning
        # 并原样返回），跳过即可。
        cur_status = state.get("status", "news_searching") or "news_searching"
        if cur_status != PHASE_INFERRING:
            sm = PredictionSM.from_db_status(cur_status)
            new_status = sm.send_event(PHASE_INFERRING)
            try:
                await update_prediction_status(snapshot_id, new_status)
            except Exception as exc:
                logger.warning(
                    "update_prediction_status(inferring) 失败 snap=%s: %s", snapshot_id, exc,
                )

        # ── 1. 拉取历史 K 线（2 年日线，ARIMA/GARCH 热身够用）──────────────────
        await emit_progress(
            snapshot_id,
            PHASE_INFERRING,
            f"正在拉取 {symbol} 的历史行情…",
            label="行情计算",
            index=0,
            total=2,
            status="inferring",
        )
        try:
            # 重试退避 + provider 兜底都在 fetch_bars 里；失败必抛 MarketDataError，
            # 成功则保证非空、date 升序、根数 ≥ MIN_BARS_FOR_FORECAST。
            bars_df = await fetch_bars(symbol, market, PREDICTION_BARS_LOOKBACK_DAYS)
        except MarketDataError as exc:
            # 诚实的失败原因：限流 / 网络不通 / 标的不存在 / 数据不足 —— 不再一律"历史行情为空"。
            logger.warning(
                "predict_compute 拉取行情失败 snap=%s symbol=%s market=%s kind=%s: %s",
                snapshot_id, symbol, market, exc.kind, exc.message,
            )
            return {
                **state,
                "error": exc.message,
                "error_kind": exc.kind,
                "status": "inferring",
            }
        except Exception as exc:
            # 铁律 #9：异常不吞（fetch_bars 已兜底包装，此处只防意外类型）
            logger.warning(
                "predict_compute 拉取行情未预期异常 snap=%s symbol=%s: %s",
                snapshot_id, symbol, exc, exc_info=True,
            )
            return {
                **state,
                "error": f"行情拉取失败：{exc}",
                "error_kind": KIND_NO_DATA,
                "status": "inferring",
            }

        # ── 2. 调用计算引擎 forecast（流式推导：公式步骤 + 逐日蜡烛）───────────
        await emit_progress(
            snapshot_id,
            PHASE_INFERRING,
            f"行情就绪 · {len(bars_df)} 根K线，正在运行 ARIMA/GARCH/事件分/蒙特卡洛…",
            label="模型推理",
            index=1,
            total=2,
            status="inferring",
        )

        async def _on_step(payload: dict) -> None:
            """engine 的流式推导回调：SSE 派发 + 逐条增量落库。

            payload["type"] 已经是 SSEEventType 枚举值（engine._step_payload /
            _candle_payload 生成，铁律 #3）。回调抛异常只会被 engine warning 掉，
            不中断计算 —— 但这里自己也兜一层，免得一次 DB 抖动吞掉整根蜡烛的 SSE。
            """
            ev_type = payload.get("type", "")
            try:
                await adispatch_custom_event("prediction_event", dict(payload))
            except Exception as exc:
                logger.warning("dispatch %s 失败 snap=%s: %s", ev_type, snapshot_id, exc)

            row = {k: v for k, v in payload.items() if k != "type"}
            if ev_type == SSEEventType.FORMULA_STEP.value:
                # 去重键 stage（= FormulaStep.key），engine 已在 payload 里带上
                await append_prediction_formula_step(snapshot_id, row)
            elif ev_type == SSEEventType.FORECAST_DAY.value:
                # 去重键 date：续跑/重试重复写不会长出 14 根蜡烛
                await append_prediction_forecast_candle(snapshot_id, row)

        try:
            result = await forecast(
                bars_df,
                float(state.get("news_sentiment") or 0.0),
                horizon,
                filing_digest=filing_digest,
                on_step=_on_step,
            )
        except Exception as exc:
            logger.warning(
                "predict_compute forecast 失败 snap=%s symbol=%s: %s",
                snapshot_id, symbol, exc, exc_info=True,
            )
            return {
                **state,
                "error": f"预测计算失败：{exc}",
                "error_kind": KIND_NO_DATA,
                "status": "inferring",
            }

        # ── 3. 持久化预测结果（独立 JSONB 字段，铁律 #2）──────────────────────
        dates = result.get("dates") or []
        point_forecast = result.get("point_forecast") or []
        p10 = result.get("p10") or []
        p50 = result.get("p50") or []
        p90 = result.get("p90") or []
        inflection_points = result.get("inflection_points") or []
        confidence = result.get("confidence")
        model_name = result.get("model_name", "")
        technical_indicators = result.get("technical_indicators") or {}
        model_params = result.get("model_params") or {}
        formula_steps = result.get("formula_steps") or []
        forecast_candles = result.get("forecast_candles") or []

        # forecast 列表：[{date, price}] 供前端逐日渲染
        forecast_list = [
            {"date": d, "price": float(p) if p is not None and pd.notna(p) else None}
            for d, p in zip(dates, point_forecast)
        ]
        confidence_band = {
            "dates": dates,
            "p10": p10,
            "p50": p50,
            "p90": p90,
        }
        predicted_price = float(point_forecast[-1]) if point_forecast else None
        predicted_range = (
            [float(p10[-1]) if p10 else None, float(p90[-1]) if p90 else None]
            if (p10 or p90)
            else None
        )
        target_date = dates[-1] if dates else None

        try:
            await update_prediction_forecast(
                snapshot_id,
                forecast=forecast_list,
                inflection_points=inflection_points,
                confidence_band=confidence_band,
                predicted_price=predicted_price,
                predicted_range=predicted_range,
                confidence=float(confidence) if confidence is not None else None,
                model_name=model_name,
                technical_indicators=technical_indicators,
                target_date=target_date,
            )
        except Exception as exc:
            logger.warning("update_prediction_forecast 失败 snap=%s: %s", snapshot_id, exc)

        # v2 产物覆盖写（流式过程已 append 过；这里兜底，单条 append 失败不至于让
        # 公式卡片 / 蜡烛在刷新后缺一块）
        try:
            await update_prediction_model_params(snapshot_id, model_params)
        except Exception as exc:
            logger.warning("update_prediction_model_params 失败 snap=%s: %s", snapshot_id, exc)
        try:
            await update_prediction_formula_steps(snapshot_id, formula_steps)
        except Exception as exc:
            logger.warning("update_prediction_formula_steps 失败 snap=%s: %s", snapshot_id, exc)
        try:
            await update_prediction_candles(snapshot_id, forecast_candles)
        except Exception as exc:
            logger.warning("update_prediction_candles 失败 snap=%s: %s", snapshot_id, exc)

        # 历史 K 线落库：已完成记录点开详情时直接从库读渲染，跳过现拉（雅虎限流会卡）。
        # 走到这里 = fetch_bars 保证 bars_df 非空 + forecast 已成功（失败早就 return 了），
        # 所以只在预测成功时落，失败记录不落 K 线。零额外网络请求：bars_df 已在内存。
        # 铁律 #9：转换/落库是锦上添花，异常只 warning，绝不挂掉整个预测。
        try:
            chart = bars_to_chart(bars_df)
            await update_prediction_historical_bars(snapshot_id, chart)
        except Exception as exc:
            logger.warning("update_prediction_historical_bars 失败 snap=%s: %s", snapshot_id, exc)

        # ── 4. 事件打点（K 线上的公告标记）+ 事件综合分 F ──────────────────────
        event_markers, event_score = await self._emit_event_markers(
            snapshot_id, filing_digest, model_params,
        )

        # ── 5. 派发 FORECAST + INFLECTION 事件（铁律 #3）──────────────────────
        try:
            await adispatch_custom_event(
                "prediction_event",
                {"type": SSEEventType.FORECAST.value, "forecast": result},
            )
        except Exception as exc:
            logger.warning("dispatch FORECAST 失败: %s", exc)

        try:
            await adispatch_custom_event(
                "prediction_event",
                {"type": SSEEventType.INFLECTION.value, "points": inflection_points},
            )
        except Exception as exc:
            logger.warning("dispatch INFLECTION 失败: %s", exc)

        # ── 6. 压缩行情摘要入 state（不把巨大 bars_df 塞 LangGraph state）─────
        try:
            last_close = float(bars_df["close"].iloc[-1]) if "close" in bars_df.columns else None
            first_date = str(bars_df["date"].iloc[0]) if "date" in bars_df.columns and len(bars_df) else ""
            last_date = str(bars_df["date"].iloc[-1]) if "date" in bars_df.columns and len(bars_df) else ""
        except Exception as exc:
            logger.warning("bars_summary 提取失败 snap=%s: %s", snapshot_id, exc)
            last_close, first_date, last_date = None, "", ""

        bars_summary = {
            "symbol": symbol,
            "bars_count": int(len(bars_df)),
            "first_date": first_date,
            "last_date": last_date,
            "last_close": last_close,
        }

        logger.info(
            "predict_compute 完成 snap=%s symbol=%s horizon=%d candles=%d steps=%d "
            "markers=%d event_score=%.4f",
            snapshot_id, symbol, horizon, len(forecast_candles), len(formula_steps),
            len(event_markers), event_score,
        )

        return {
            **state,
            "bars_summary": bars_summary,
            "forecast": result,
            "event_score": event_score,
            "status": "inferring",
        }

    # ── 事件打点 ──────────────────────────────────────────────────────────────

    async def _emit_event_markers(
        self,
        snapshot_id: str,
        filing_digest: list[dict],
        model_params: dict,
    ) -> tuple[list[dict], float]:
        """公告 → K 线打点（EVENT_MARKER）+ 事件综合分 F 落库。

        F 的单一真相源是 engine（model_params.filing_score，engine 内部就是调
        announcements.aggregate_event_score 算的）；model_params 缺字段时才自己再算一次，
        绝不在这里重新实现一遍公式。
        """
        if not filing_digest:
            return [], 0.0

        try:
            markers = to_event_markers(filing_digest, limit=EVENT_MARKER_LIMIT)
        except Exception as exc:
            logger.warning("to_event_markers 失败 snap=%s: %s", snapshot_id, exc)
            return [], 0.0

        score = model_params.get("filing_score")
        if score is None:
            try:
                score, _scores, _weights = aggregate_event_score(filing_digest)
            except Exception as exc:
                logger.warning("aggregate_event_score 失败 snap=%s: %s", snapshot_id, exc)
                score = 0.0
        event_score = float(score or 0.0)

        for m in markers:
            try:
                await adispatch_custom_event(
                    "prediction_event",
                    {"type": SSEEventType.EVENT_MARKER.value, **m},
                )
            except Exception as exc:
                logger.warning("dispatch EVENT_MARKER 失败 snap=%s: %s", snapshot_id, exc)

        try:
            await update_prediction_event_markers(snapshot_id, markers, event_score=event_score)
        except Exception as exc:
            logger.warning("update_prediction_event_markers 失败 snap=%s: %s", snapshot_id, exc)

        return markers, event_score
