"""综合分析节点 — LLM 基于技术指标 + 资讯情绪 + 预测结果输出分析。

镜像 graph/nodes/quant/analyze_node.py 的形态：
  - llm.astream 流式（铁律 #5，禁 ainvoke）
  - BaseNode.emit_thinking(name, "reasoning"|"content", delta, None)（铁律 #7）
  - _THINK_PREFIX 剥离（推理模型 thinking token）
  - 容错 JSON 解析（_parse_analysis_json）
  - _build_segments 构造 thinking_segments

step_index 始终为 None（predict_analyze 不属于主对话计划体系）。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import CHAT_MODEL
from graph.nodes.base import BaseNode
from graph.nodes.prediction.preflight_node import PHASE_INFERRING, emit_progress
from llm.chat import get_chat_llm

if TYPE_CHECKING:
    from graph.prediction_agent import PredictState

logger = logging.getLogger("graph.nodes.prediction.analyze")


_SYSTEM_PROMPT = (
    "你是量化预测分析师。基于技术指标+资讯情绪+统计模型预测结果，按 JSON 输出 "
    "{analysis: 2-4句, risk_notes: [≤3条]}。"
    "附免责声明'预测仅供参考不构成投资建议'。"
    "禁止输出 Markdown 代码块或多余文字。"
)


class AnalyzeNode(BaseNode):
    """预测结果 LLM 综合分析节点。"""

    @property
    def name(self) -> str:
        return "predict_analyze"

    async def execute(self, state: "PredictState") -> dict:
        snapshot_id = state.get("snapshot_id", "")
        symbol = state.get("symbol", "")
        market = state.get("market", "cn_a")

        # ── 上游已失败 → 原样返回，一个 LLM token 都不烧 ───────────────────────
        # 旧行为：compute 已置 error，analyze 照样跑一次完整 LLM —— 用户先看见分析流
        # 哗哗流出来，几十秒后才弹一个红色报错，观感极差且白白浪费 token。
        # 正常路径由编排层的条件边短路到 END，这里是双保险（也覆盖 background_predict）。
        if state.get("error"):
            logger.info(
                "predict_analyze 跳过（上游已失败）snap=%s kind=%s: %s",
                snapshot_id, state.get("error_kind", ""), state.get("error", ""),
            )
            return dict(state)

        forecast = state.get("forecast") or {}
        news_digest = state.get("news_digest") or []
        news_sentiment = state.get("news_sentiment", 0.0)
        technical_indicators = forecast.get("technical_indicators") or {}

        # news_digest 压缩为摘要（dimension + sentiment + reason），控制 token
        news_summary = [
            {
                "dimension": d.get("dimension", ""),
                "sentiment": d.get("sentiment", 0.0),
                "sentiment_reason": d.get("sentiment_reason", ""),
            }
            for d in news_digest
        ]

        forecast_payload = {
            "point_forecast": forecast.get("point_forecast", []),
            "p10": forecast.get("p10", []),
            "p90": forecast.get("p90", []),
            "inflection_points": forecast.get("inflection_points", []),
            "confidence": forecast.get("confidence", 0.0),
        }

        user_prompt = json.dumps(
            {
                "symbol": symbol,
                "market": market,
                "technical_indicators": technical_indicators,
                "news_digest": news_summary,
                "news_sentiment": news_sentiment,
                "forecast": forecast_payload,
            },
            ensure_ascii=False,
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        llm = get_chat_llm(CHAT_MODEL, temperature=0.3)

        await emit_progress(
            snapshot_id,
            PHASE_INFERRING,
            "预测完成，正在生成综合分析…",
            label="综合分析",
            index=2,
            total=2,
            status="inferring",
        )

        # ── 流式调用：thinking 走 emit_thinking(reasoning) / content 走 emit_thinking(content) ──
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        token_count = 0

        try:
            async for delta in llm.astream(messages, temperature=0.3, timeout=120.0):
                if isinstance(delta, dict) and "usage" in delta:
                    continue
                if not isinstance(delta, str):
                    continue
                token_count += 1
                if delta.startswith(BaseNode._THINK_PREFIX):
                    text = delta[len(BaseNode._THINK_PREFIX):]
                    thinking_parts.append(text)
                    await BaseNode.emit_thinking(self.name, "reasoning", text, None)
                else:
                    content_parts.append(delta)
                    await BaseNode.emit_thinking(self.name, "content", delta, None)
        except Exception as exc:
            partial = "".join(content_parts)
            logger.warning(
                "predict_analyze 流式失败 snapshot=%s tokens=%d partial_len=%d err=%s",
                snapshot_id, token_count, len(partial), exc,
            )
            # 铁律 #9：异常不吞；部分内容仍尝试解析，给上层最大可用信息
            analysis_partial, risk_partial = _parse_analysis_json(partial)
            return {
                "analysis": analysis_partial,
                "risk_notes": risk_partial,
                "thinking_segments": _build_segments("".join(thinking_parts), partial),
                "full_thinking": "".join(thinking_parts),
                "full_content": partial,
                "error": str(exc),
            }

        full_content = "".join(content_parts)
        full_thinking = "".join(thinking_parts)
        analysis, risk_notes = _parse_analysis_json(full_content)

        logger.info(
            "predict_analyze 完成 snapshot=%s tokens=%d content_len=%d thinking_len=%d risk=%d",
            snapshot_id, token_count, len(full_content), len(full_thinking), len(risk_notes),
        )

        return {
            "analysis": analysis,
            "risk_notes": risk_notes,
            "thinking_segments": _build_segments(full_thinking, full_content),
            "full_thinking": full_thinking,
            "full_content": full_content,
        }


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _build_segments(reasoning: str, content: str) -> list[dict]:
    """构造 spec §模型思考流程的结构化段（与 thinking_segments 同形）。"""
    segments: list[dict] = []
    if reasoning:
        segments.append({
            "node": "predict_analyze",
            "step_index": None,
            "phase": "reasoning",
            "content": reasoning,
        })
    if content:
        segments.append({
            "node": "predict_analyze",
            "step_index": None,
            "phase": "content",
            "content": content,
        })
    return segments


def _parse_analysis_json(raw: str) -> tuple[str, list[str]]:
    """容错解析 LLM JSON 输出（沿用 quant analyze_node 的实现）。"""
    if not raw:
        return "", []
    try:
        return _extract_fields(json.loads(raw))
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        snippet = raw[start : end + 1]
        try:
            return _extract_fields(json.loads(snippet))
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
