"""量化 LangGraph — 拆成两条独立 graph：

  - screen_graph：纯因子管线（同步 REST 用），不调用 LLM，秒级返回
  - analyze_graph：LLM 流式洞察（SSE 用），消费已写入 DB 的快照

设计取舍：
  - 把 LLM 从 /screen 拆出去 = 前端先看到表格（5-10s），再异步流入洞察（5-30s）
  - LLM 调用走流式（spec.md 铁律 #6），通过 yield SSE chunk 推到前端
  - 风险提示用 JSON 结构化输出，避免字符串切割误差
  - analyze_graph 是真正的 LangGraph，通过 astream_events 驱动，节点内 emit_thinking
    保证 spec §模型思考流程铁律 #1（thinking 必须走 BaseNode.emit_thinking 统一入口）
"""
from __future__ import annotations

import logging
import time
from typing import AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from db.quant_store import update_quant_analysis, update_quant_snapshot
from graph.nodes.quant import AnalyzeNode
from graph.nodes.quant.analyze_node import AnalyzeState, _parse_analysis_json  # 向后兼容旧测试
from quant.domain import ScreenCriteria
from quant.service import get_service

__all__ = [
    "screen_graph", "analyze_graph", "quant_graph",
    "background_screen", "stream_analyze",
    "_parse_analysis_json",  # 旧测试入口（迁移到 analyze_node 后保留兼容）
]

logger = logging.getLogger("graph.quant_agent")


# ── screen_graph：因子管线（无 LLM，支持异步后台运行） ─────────────────────────

class ScreenState(TypedDict, total=False):
    client_id: str
    user_id: str
    criteria: dict
    snapshot_id: str
    rows: list[dict]
    provider_trace: list[dict]
    weights: dict
    universe_size: int
    warnings: list[str]
    as_of_date: str
    generated_at: float
    status: str
    error: str


async def _run_screening(state: ScreenState) -> ScreenState:
    try:
        service = get_service()
        criteria_obj = ScreenCriteria.model_validate(state["criteria"])
        result = await service.screen(criteria_obj, snapshot_id=state.get("snapshot_id"))
        return {
            **state,
            "snapshot_id": result.snapshot_id,
            "rows": [r.model_dump() for r in result.rows],
            "provider_trace": [t.model_dump() for t in result.provider_trace],
            "weights": result.weights,
            "universe_size": result.universe_size,
            "warnings": result.warnings,
            "as_of_date": result.as_of_date,
            "generated_at": result.generated_at,
            "status": "DONE",
        }
    except Exception as exc:
        logger.exception("选股管线失败")
        return {**state, "error": str(exc), "status": "FAILED"}


async def _persist_snapshot(state: ScreenState) -> ScreenState:
    """计算完成后更新 DB 中的记录。"""
    sid = state.get("snapshot_id")
    if not sid:
        return state
    
    status = state.get("status", "DONE")
    if state.get("error"):
        status = "FAILED"

    try:
        await update_quant_snapshot(
            snapshot_id=sid,
            rows=state.get("rows") or [],
            provider_trace=state.get("provider_trace") or [],
            status=status,
            warnings=state.get("warnings"),
        )
    except Exception as exc:
        logger.warning("快照更新库失败: %s", exc)
    return state


def _build_screen_graph():
    builder = StateGraph(ScreenState)
    builder.add_node("run_screening", _run_screening)
    builder.add_node("persist_snapshot", _persist_snapshot)

    builder.add_edge(START, "run_screening")
    builder.add_edge("run_screening", "persist_snapshot")
    builder.add_edge("persist_snapshot", END)
    return builder.compile()


screen_graph = _build_screen_graph()


async def background_screen(snapshot_id: str, client_id: str, criteria: dict, user_id: str = ""):
    """在后台运行选股流程的入口。"""
    t0 = time.perf_counter()
    logger.info("🚀 [后台任务] 启动选股 snapshot_id=%s client_id=%s user_id=%s", snapshot_id, client_id, user_id)
    try:
        state = {
            "snapshot_id": snapshot_id,
            "client_id": client_id,
            "user_id": user_id,
            "criteria": criteria,
        }
        await screen_graph.ainvoke(state)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("✅ [后台任务] 选股完成 snapshot_id=%s 耗时=%.0fms", snapshot_id, elapsed)
    except Exception as exc:
        logger.exception("❌ [后台任务] 异常 snapshot_id=%s", snapshot_id)
        await update_quant_snapshot(snapshot_id, [], [], status="FAILED")


# ── analyze_graph：LangGraph 节点 + astream_events 驱动 ──────────────────────
# spec §模型思考流程铁律 #1：thinking 必须走 BaseNode.emit_thinking 统一入口，
# 而 emit_thinking 内部用 adispatch_custom_event 派发，要求在 LangGraph 执行链路中。
# 因此把 analyze 包成 StateGraph，由 astream_events 接到 llm_thinking 自定义事件。

_analyze_node = AnalyzeNode()


def _build_analyze_graph():
    builder = StateGraph(AnalyzeState)
    builder.add_node("quant_analyze", _analyze_node.execute)
    builder.add_edge(START, "quant_analyze")
    builder.add_edge("quant_analyze", END)
    return builder.compile()


analyze_graph = _build_analyze_graph()


async def stream_analyze(
    snapshot_id: str,
    criteria: dict,
    rows: list[dict],
    *,
    top_n_for_llm: int = 5,
) -> AsyncGenerator[dict, None]:
    """流式分析。yield 事件 dict，由路由层包成 SSE：

      {"event": "thinking", "text": "..."}                    # reasoning_content（推理过程）
      {"event": "delta",    "text": "..."}                    # content（最终 JSON 增量）
      {"event": "done",     "analysis": ..., "risk_notes": [...], "thinking_segments": [...]}
      {"event": "error",    "message": "..."}

    完成后通过 update_quant_analysis 回写 analysis / risk_notes / thinking_segments 到 DB。
    """
    if not rows:
        empty_msg = "未筛选到任何符合条件的股票。"
        yield {
            "event": "done",
            "analysis": empty_msg,
            "risk_notes": [],
            "thinking_segments": [],
        }
        try:
            await update_quant_analysis(snapshot_id, empty_msg, [], thinking_segments=[])
        except Exception as exc:
            logger.warning("回写空分析失败: %s", exc)
        return

    state: AnalyzeState = {
        "snapshot_id": snapshot_id,
        "criteria": criteria,
        "rows": rows,
        "top_n_for_llm": top_n_for_llm,
    }

    final_state: dict = {}
    t0 = time.perf_counter()

    try:
        # version="v2" 是 LangGraph 推荐的事件流格式
        async for ev in analyze_graph.astream_events(state, version="v2"):
            ev_type = ev.get("event", "")

            # 节点内通过 emit_thinking 派发的自定义事件
            if ev_type == "on_custom_event" and ev.get("name") == "llm_thinking":
                data = ev.get("data") or {}
                phase = data.get("phase", "reasoning")
                delta = data.get("delta", "")
                if not delta:
                    continue
                if phase == "content":
                    yield {"event": "delta", "text": delta}
                else:
                    yield {"event": "thinking", "text": delta}
                continue

            # 节点结束：捕获最终 state（包含 analysis / risk_notes / thinking_segments）
            if ev_type == "on_chain_end" and ev.get("name") == "quant_analyze":
                output = (ev.get("data") or {}).get("output") or {}
                if isinstance(output, dict):
                    final_state.update(output)

            # graph 结束：兜底捕获最终 state
            if ev_type == "on_chain_end" and ev.get("name") == "LangGraph":
                output = (ev.get("data") or {}).get("output") or {}
                if isinstance(output, dict):
                    final_state.update(output)
    except Exception as exc:
        logger.warning("analyze_graph 流式异常 snapshot=%s err=%s", snapshot_id, exc)
        yield {"event": "error", "message": f"LLM 分析失败：{exc}"}
        return

    analysis = final_state.get("analysis", "")
    risk_notes = final_state.get("risk_notes", []) or []
    thinking_segments = final_state.get("thinking_segments", []) or []
    err = final_state.get("error", "")

    elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "stream_analyze 完成 snapshot=%s elapsed=%.0fms risk=%d thinking_segs=%d err=%s",
        snapshot_id, elapsed, len(risk_notes), len(thinking_segments), err or "-",
    )

    try:
        await update_quant_analysis(
            snapshot_id, analysis, risk_notes, thinking_segments=thinking_segments,
        )
    except Exception as exc:
        logger.warning("回写分析失败 snapshot=%s err=%s", snapshot_id, exc)

    if err:
        yield {"event": "error", "message": err}
        return

    yield {
        "event": "done",
        "analysis": analysis,
        "risk_notes": risk_notes,
        "thinking_segments": thinking_segments,
    }


# ── 向后兼容：保留旧名 quant_graph 指向 screen_graph ──────────────────────────
quant_graph = screen_graph
