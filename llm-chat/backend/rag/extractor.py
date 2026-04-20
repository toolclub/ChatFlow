"""
事实抽取器：把一轮对话压缩成若干条"可长期复用的事实"。

触发方式：save_response 之后的 extract_memory 节点以 fire-and-forget 形式调用。
调用 SUMMARY_MODEL（低温度），返回结构化 JSON。

spec 铁律 #6 说明：
    "所有 LLM 调用必须流式"针对的是向用户展示思考/内容的前台节点。
    本模块运行在 save_response 之后的后台 Task，不产出任何 SSE 事件，
    调用方也不消费增量 token。这里使用 `LLMClient.ainvoke()` 获取
    完整 JSON，避免自己拼流式片段。compressor.py 同理（已是先例）。

spec 铁律 #9 说明：
    所有异常必须至少 logger.warning，这里全部按规定处理，不吞异常。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from config import settings
from llm.chat import get_summary_llm
from memory.core_memory import ensure_core_memory
from memory.schema import Conversation
from prompts import load_prompt as _lp
from rag.fact_schema import FactRecord, VALID_FACT_TYPES, FACT_TYPE_KNOWLEDGE

logger = logging.getLogger("rag.extractor")

# ── 抽取结果过滤阈值 ───────────────────────────────────────────────────────────
# 低于该置信度的事实一律丢弃。宁可漏抽，也不要把猜测污染长期记忆。
MIN_CONFIDENCE = float(getattr(settings, "fact_min_confidence", 0.7))
# 单轮最多抽多少条事实（防止模型"一轮抽 20 条"的灾难）
MAX_FACTS_PER_TURN = int(getattr(settings, "fact_max_per_turn", 6))
# 每条 fact 字符串的最大长度（超过截断）
MAX_FACT_LEN = 200


@dataclass
class ExtractionRequest:
    conv_id: str
    user_id: str
    user_msg: str
    assistant_msg: str
    tool_summary: str = ""
    source_msg_id: int = 0
    core_memory: dict | None = None


async def extract_facts_from_turn(req: ExtractionRequest) -> list[FactRecord]:
    """
    从一轮对话抽取事实列表。

    返回已过滤（低置信度 / 空内容 / 非法 type）的 FactRecord 列表。
    任何失败都不会向外抛出 —— 调用方只关心"能拿几条就用几条"。
    """
    user_msg = (req.user_msg or "").strip()
    asst_msg = (req.assistant_msg or "").strip()
    if not user_msg or not asst_msg:
        return []

    core_snapshot = _render_core_snapshot(req.core_memory or {})
    tool_section = (
        f"\n工具调用摘要：\n{req.tool_summary.strip()[:1500]}\n"
        if req.tool_summary and req.tool_summary.strip()
        else ""
    )

    try:
        prompt = _lp(
            "nodes/fact_extractor",
            core_memory_snapshot=core_snapshot,
            user_msg=user_msg[:1500],
            assistant_msg=asst_msg[:2000],
            tool_summary_section=tool_section,
        )
    except Exception as exc:
        logger.warning("加载 fact_extractor prompt 失败: %s", exc)
        return []

    raw = await _call_llm(prompt, conv_id=req.conv_id)
    if not raw:
        return []

    parsed = _safe_parse(raw)
    if parsed is None:
        logger.warning(
            "fact_extractor 返回不是合法 JSON | conv=%s | raw=%.200s",
            req.conv_id, raw,
        )
        return []

    raw_facts = parsed.get("facts") if isinstance(parsed, dict) else None
    if not isinstance(raw_facts, list):
        return []

    results: list[FactRecord] = []
    for item in raw_facts[:MAX_FACTS_PER_TURN]:
        rec = _coerce_fact(item, req)
        if rec:
            results.append(rec)

    logger.info(
        "fact_extractor | conv=%s | user_id=%s | raw=%d → kept=%d",
        req.conv_id, req.user_id, len(raw_facts), len(results),
    )
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 内部实现
# ══════════════════════════════════════════════════════════════════════════════

async def _call_llm(prompt: str, conv_id: str) -> str:
    """调用 SUMMARY_MODEL；失败时返回空串。"""
    try:
        llm = get_summary_llm()
        messages = [
            {"role": "system", "content": "你是严格按 JSON 规范输出的抽取器。只输出 JSON 对象，不要任何其他文字。"},
            {"role": "user",   "content": prompt},
        ]
        completion = await llm.ainvoke(messages, temperature=0.1, timeout=60.0)
        content = completion.choices[0].message.content or ""
        return content.strip()
    except Exception as exc:
        logger.warning(
            "fact_extractor LLM 调用失败（本轮放弃抽取）| conv=%s | error=%s",
            conv_id, exc,
        )
        return ""


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _safe_parse(raw: str) -> Optional[dict]:
    """尝试把模型输出解析成 dict；容忍 ```json 围栏等常见脏数据。"""
    text = raw.strip()
    if text.startswith("```"):
        # 去 ```json fenced block
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _coerce_fact(item: Any, req: ExtractionRequest) -> Optional[FactRecord]:
    """把 LLM 返回的一个 fact dict 校验并转换成 FactRecord。"""
    if not isinstance(item, dict):
        return None
    fact = (item.get("fact") or "").strip()
    if not fact:
        return None
    if len(fact) > MAX_FACT_LEN:
        fact = fact[:MAX_FACT_LEN].rstrip()

    fact_type = (item.get("type") or "").strip().lower()
    if fact_type not in VALID_FACT_TYPES:
        # 非法 type 时降级到 knowledge，但提示日志
        logger.debug("fact type 非法，降级为 knowledge: %r", fact_type)
        fact_type = FACT_TYPE_KNOWLEDGE

    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    confidence = max(0.0, min(1.0, confidence))

    return FactRecord(
        fact=fact,
        fact_type=fact_type,
        confidence=confidence,
        user_id=req.user_id,
        conv_id=req.conv_id,
        source_msg_id=req.source_msg_id,
        source_user_msg=req.user_msg[:400],
        source_assistant_msg=req.assistant_msg[:400],
    )


def _render_core_snapshot(core: dict) -> str:
    """
    把 core_memory 渲染成「已知事实」块供抽取器参考，
    避免反复抽取 user_profile / project_rules / learned_preferences / current_task
    中已经显式写入的内容。
    """
    normalized = ensure_core_memory(core)
    lines: list[str] = []
    for field, label in (
        ("user_profile",         "身份"),
        ("project_rules",        "规则"),
        ("learned_preferences",  "偏好"),
    ):
        items = normalized.get(field) or []
        for it in items:
            lines.append(f"- [{label}] {it}")
    task = normalized.get("current_task") or ""
    if task:
        lines.append(f"- [任务] {task}")
    return "\n".join(lines) if lines else "(当前没有已知事实)"


def snapshot_core(conv: Conversation | None) -> dict:
    """便捷函数：从 Conversation 取出 core_memory 快照（浅拷贝）。"""
    return dict(getattr(conv, "core_memory", {}) or {})
