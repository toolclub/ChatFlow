"""量化 LangGraph — 拆成两条独立 graph：

  - screen_graph：纯因子管线（同步 REST 用），不调用 LLM，秒级返回
  - analyze_graph：LLM 流式洞察（SSE 用），消费已写入 DB 的快照

设计取舍：
  - 把 LLM 从 /screen 拆出去 = 前端先看到表格（5-10s），再异步流入洞察（5-30s）
  - LLM 调用走流式（spec.md 铁律 #6），通过 yield SSE chunk 推到前端
  - 风险提示用 JSON 结构化输出，避免字符串切割误差
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from config import CHAT_MODEL
from db.quant_store import save_quant_snapshot, update_quant_analysis, update_quant_snapshot
from llm.chat import get_chat_llm
from quant.domain import ScreenCriteria
from quant.service import get_service

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


# ── analyze 流式：直接生成器，不入 LangGraph ──────────────────────────────────
# spec 铁律 #6：LLM 调用必须流式。这里直接走 LLMClient.astream，
# 同时 yield 给上层封装成 SSE。完成后回写 DB。

_ANALYZE_SYSTEM = (
    "你是一名专业的量化分析师。基于给定的选股结果，按以下 JSON Schema 严格输出（不要 Markdown 代码块）：\n"
    "{\n"
    '  "analysis": "2-4 句话总结候选标的的整体特征（行业/估值/动量/集中度等）",\n'
    '  "risk_notes": ["不超过 3 条独立的风险提示，每条短句"]\n'
    "}\n"
    "禁止输出多余文字。"
)


async def stream_analyze(
    snapshot_id: str,
    criteria: dict,
    rows: list[dict],
    *,
    top_n_for_llm: int = 5,
) -> AsyncGenerator[dict, None]:
    """
    流式分析。yield 事件 dict，由路由层包成 SSE：

      {"event": "delta", "text": "..."}            # 增量 token
      {"event": "done",  "analysis": ..., "risk_notes": [...]}
      {"event": "error", "message": "..."}

    完成后会把结构化结果回写 DB（update_quant_analysis）。
    """
    if not rows:
        yield {
            "event": "done",
            "analysis": "未筛选到任何符合条件的股票。",
            "risk_notes": [],
        }
        try:
            await update_quant_analysis(snapshot_id, "未筛选到任何符合条件的股票。", [])
        except Exception as exc:
            logger.warning("回写空分析失败: %s", exc)
        return

    payload_rows = [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "total": r.get("total"),
            "technical": r.get("technical"),
            "fundamental": r.get("fundamental"),
            "liquidity": r.get("liquidity"),
            "reasons": r.get("reasons", []),
        }
        for r in rows[:top_n_for_llm]
    ]
    user_prompt = (
        f"选股条件：{json.dumps(criteria, ensure_ascii=False)}\n"
        f"Top{len(payload_rows)} 结果：{json.dumps(payload_rows, ensure_ascii=False)}\n"
        "请输出 JSON。"
    )

    messages = [
        {"role": "system", "content": _ANALYZE_SYSTEM},
        {"role": "user",   "content": user_prompt},
    ]

    llm = get_chat_llm(CHAT_MODEL, temperature=0.3)
    buffer_parts: list[str] = []
    t0 = time.perf_counter()

    try:
        async for delta in llm.astream(messages, temperature=0.3, timeout=120.0):
            if not isinstance(delta, str):
                continue
            # 推理 token（GLM/DeepSeek-R1）以 \x00THINK\x00 前缀，过滤掉只取最终内容
            if delta.startswith("\x00THINK\x00"):
                continue
            buffer_parts.append(delta)
            yield {"event": "delta", "text": delta}
    except Exception as exc:
        logger.warning("LLM 流式失败 snapshot=%s err=%s", snapshot_id, exc)
        yield {"event": "error", "message": f"LLM 分析失败：{exc}"}
        return

    raw = "".join(buffer_parts).strip()
    analysis, risk_notes = _parse_analysis_json(raw)
    elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "stream_analyze 完成 snapshot=%s tokens=%d elapsed=%.0fms risk=%d",
        snapshot_id, len(buffer_parts), elapsed, len(risk_notes),
    )

    try:
        await update_quant_analysis(snapshot_id, analysis, risk_notes)
    except Exception as exc:
        logger.warning("回写分析失败 snapshot=%s err=%s", snapshot_id, exc)

    yield {"event": "done", "analysis": analysis, "risk_notes": risk_notes}


def _parse_analysis_json(raw: str) -> tuple[str, list[str]]:
    """容错解析 LLM JSON 输出：
      1. 优先 json.loads 整段
      2. 失败则提取首对 { ... }
      3. 再失败则把整段当 analysis、空风险列表
    """
    if not raw:
        return "", []
    try:
        obj = json.loads(raw)
        return _extract_fields(obj)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        snippet = raw[start : end + 1]
        try:
            obj = json.loads(snippet)
            return _extract_fields(obj)
        except json.JSONDecodeError:
            pass

    return raw, []


def _extract_fields(obj) -> tuple[str, list[str]]:
    if not isinstance(obj, dict):
        return str(obj), []
    analysis = str(obj.get("analysis", "")).strip()
    rn = obj.get("risk_notes") or []
    if isinstance(rn, str):
        rn = [rn]
    risk_notes = [str(x).strip() for x in rn if str(x).strip()][:5]
    return analysis, risk_notes


# ── 向后兼容：保留旧名 quant_graph 指向 screen_graph ──────────────────────────
quant_graph = screen_graph
