"""量化分析节点 — 在 snapshot 上跑 LLM 洞察。

合规性（spec.md）：
  - 思考流程铁律 #1：通过 BaseNode.emit_thinking 推送 reasoning + content（结构化分段）
  - 铁律 #6：astream 流式调用，禁用 ainvoke
  - 铁律 #1（数据/状态）：分析结果由本节点累积内容后由编排层落库（不读模型文本判状态）

phase 划分：
  - reasoning：推理模型的 reasoning_content（GLM-Z1 / DeepSeek-R1）；仅展示
  - content：最终 JSON 文本（外显）；编排层在节点结束后解析为 analysis + risk_notes

step_index 始终为 None（quant_analyze 不属于主对话计划体系）。
"""
from __future__ import annotations

import json
import logging
from typing import TypedDict

from config import CHAT_MODEL
from graph.nodes.base import BaseNode
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.nodes.quant.analyze")


# ── 节点 state（不复用 GraphState，量化分析独立） ──────────────────────────────

class AnalyzeState(TypedDict, total=False):
    snapshot_id: str
    criteria: dict
    rows: list[dict]
    top_n_for_llm: int

    # 输出
    analysis: str
    risk_notes: list[str]
    thinking_segments: list[dict]
    full_thinking: str
    full_content: str
    error: str


_SYSTEM_PROMPT = (
    "你是一名专业的量化分析师。基于给定的选股结果，按以下 JSON Schema 严格输出（不要 Markdown 代码块）：\n"
    "{\n"
    '  "analysis": "2-4 句话总结候选标的的整体特征（行业/估值/动量/集中度等）",\n'
    '  "risk_notes": ["不超过 3 条独立的风险提示，每条短句"]\n'
    "}\n"
    "禁止输出多余文字。"
)


class AnalyzeNode(BaseNode):
    """量化 snapshot LLM 分析节点。"""

    @property
    def name(self) -> str:
        return "quant_analyze"

    async def execute(self, state: AnalyzeState) -> dict:
        rows = state.get("rows") or []
        criteria = state.get("criteria") or {}
        top_n = int(state.get("top_n_for_llm") or 5)
        snapshot_id = state.get("snapshot_id", "")

        if not rows:
            empty_msg = "未筛选到任何符合条件的股票。"
            return {
                "analysis": empty_msg,
                "risk_notes": [],
                "thinking_segments": [],
                "full_thinking": "",
                "full_content": "",
            }

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
            for r in rows[:top_n]
        ]
        user_prompt = (
            f"选股条件：{json.dumps(criteria, ensure_ascii=False)}\n"
            f"Top{len(payload_rows)} 结果：{json.dumps(payload_rows, ensure_ascii=False)}\n"
            "请输出 JSON。"
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ]

        llm = get_chat_llm(CHAT_MODEL, temperature=0.3)

        # ── 流式调用：thinking 走 emit_thinking(reasoning) / content 走 emit_thinking(content） ──
        # spec §节点覆盖矩阵：quant_analyze 节点 reasoning + content 都进 thinking 区
        # （类似 vision / planner — 量化分析没有"外显正文"概念，最终 JSON 由编排层解析）
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
                "quant_analyze 流式失败 snapshot=%s tokens=%d partial_len=%d err=%s",
                snapshot_id, token_count, len(partial), exc,
            )
            # 部分内容仍尝试解析，给上层最大可用信息（铁律 #9：异常不吞）
            analysis_partial, risk_partial = _parse_analysis_json(partial)
            return {
                "analysis": analysis_partial,
                "risk_notes": risk_partial,
                "thinking_segments": _build_segments(
                    "".join(thinking_parts), partial,
                ),
                "full_thinking": "".join(thinking_parts),
                "full_content": partial,
                "error": str(exc),
            }

        full_content = "".join(content_parts)
        full_thinking = "".join(thinking_parts)
        analysis, risk_notes = _parse_analysis_json(full_content)

        logger.info(
            "quant_analyze 完成 snapshot=%s tokens=%d content_len=%d thinking_len=%d risk=%d",
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
    """构造 spec §模型思考流程的结构化段（和 messages.thinking_segments 同形）。"""
    segments: list[dict] = []
    if reasoning:
        segments.append({
            "node": "quant_analyze",
            "step_index": None,
            "phase": "reasoning",
            "content": reasoning,
        })
    if content:
        segments.append({
            "node": "quant_analyze",
            "step_index": None,
            "phase": "content",
            "content": content,
        })
    return segments


def _parse_analysis_json(raw: str) -> tuple[str, list[str]]:
    """容错解析 LLM JSON 输出（沿用 quant_agent.py 旧实现）。"""
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
