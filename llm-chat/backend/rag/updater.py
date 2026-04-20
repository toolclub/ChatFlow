"""
事实更新器：Mem0 风格的 ADD / UPDATE / DELETE / NONE 决策层。

调用链：
    extractor.extract_facts_from_turn() → list[FactRecord]
    for fact in facts:
        updater.ingest_fact(fact)   # 对每条事实单独决策

决策流程：
    1. 用 fact.fact 文本向量化，在同 user_id（或同 conv_id）内召回 top_k 相似候选
    2. 候选为空或最高分 < FACT_UPDATE_SIM_THRESHOLD → 直接 ADD
    3. 有高相似候选 → 调用 SUMMARY_MODEL 仲裁 ADD/UPDATE/DELETE/NONE
    4. 执行对应 Qdrant 操作（upsert / mark_superseded + upsert / delete_point / skip）

spec 铁律说明：
    * #6（LLM 流式）：本模块运行在后台，无 SSE，使用 ainvoke（与 compressor / extractor 同）。
    * #9（异常处理）：所有失败 logger.warning，调用方用 fire-and-forget 消化。
"""
from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Optional

from config import (
    FACT_UPDATE_CANDIDATE_TOP_K,
    FACT_UPDATE_SIM_THRESHOLD,
)
from llm.chat import get_summary_llm
from prompts import load_prompt as _lp
from rag import retriever as rag_retriever
from rag.fact_schema import FactRecord

logger = logging.getLogger("rag.updater")


class Decision(str, Enum):
    ADD    = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NONE   = "NONE"


async def ingest_fact(fact: FactRecord) -> Decision:
    """
    把一条新事实写入记忆库（或合并到已有事实）。
    返回实际执行的决策，便于日志/测试断言。
    """
    if not fact.fact.strip():
        return Decision.NONE

    # 1) 召回候选
    candidates = await rag_retriever.search_fact_candidates(
        user_id=fact.user_id,
        conv_id=fact.conv_id,
        query=fact.fact,
        top_k=FACT_UPDATE_CANDIDATE_TOP_K,
        score_threshold=0.0,  # 这里不过滤，让仲裁拿到所有语义邻居
    )

    # 2) 快速路径：无高相似候选 → 直接 ADD
    max_score = max((c.score for c in candidates), default=0.0)
    if not candidates or max_score < FACT_UPDATE_SIM_THRESHOLD:
        new_id = await rag_retriever.upsert_fact(fact)
        logger.info(
            "ingest_fact ADD (no conflict) | user=%s | score=%.3f | id=%d | fact=%.80s",
            fact.user_id, max_score, new_id, fact.fact,
        )
        return Decision.ADD

    # 3) 过滤掉已被 superseded 的候选（它们只是历史版本，不应再被覆盖）
    live_candidates = [c for c in candidates if not _is_superseded(c)]
    if not live_candidates:
        new_id = await rag_retriever.upsert_fact(fact)
        logger.info(
            "ingest_fact ADD (all candidates superseded) | user=%s | id=%d",
            fact.user_id, new_id,
        )
        return Decision.ADD

    # 4) 仲裁路径：LLM 判决
    decision, target_id, reason = await _arbitrate(fact, live_candidates)

    if decision == Decision.NONE:
        logger.info(
            "ingest_fact NONE | user=%s | target=%d | reason=%s | fact=%.60s",
            fact.user_id, target_id, reason, fact.fact,
        )
        return Decision.NONE

    if decision == Decision.DELETE:
        if target_id:
            ok = await rag_retriever.delete_point(target_id)
            logger.info(
                "ingest_fact DELETE | user=%s | target=%d | ok=%s | reason=%s | new_fact=%.60s",
                fact.user_id, target_id, ok, reason, fact.fact,
            )
        return Decision.DELETE

    if decision == Decision.UPDATE:
        # 先写入新事实，再把旧事实标记为 superseded_by = new_id
        # 顺序重要：若先标记再写入失败，旧事实会永久失效 → 留下"记忆空洞"
        new_id = await rag_retriever.upsert_fact(fact)
        if new_id and target_id:
            await rag_retriever.mark_superseded(target_id, new_id)
        logger.info(
            "ingest_fact UPDATE | user=%s | target=%d → new=%d | reason=%s | fact=%.60s",
            fact.user_id, target_id, new_id, reason, fact.fact,
        )
        return Decision.UPDATE

    # Decision.ADD（LLM 明确让我们新增）
    new_id = await rag_retriever.upsert_fact(fact)
    logger.info(
        "ingest_fact ADD (arbitrated) | user=%s | id=%d | reason=%s | fact=%.60s",
        fact.user_id, new_id, reason, fact.fact,
    )
    return Decision.ADD


# ══════════════════════════════════════════════════════════════════════════════
# 内部
# ══════════════════════════════════════════════════════════════════════════════

def _is_superseded(hit) -> bool:
    """召回的候选是否已经被替代。"""
    # FactHit 是轻量对象，record 只有可见字段；superseded_by 不在 FactRecord
    # 构造时保留在 payload，这里从 hit 的 record.superseded_by 读
    return bool(getattr(hit.record, "superseded_by", 0))


def _render_candidate_block(candidates) -> str:
    """构造 prompt 中的候选块。"""
    lines: list[str] = []
    for c in candidates:
        rec = c.record
        tags = [f"id={c.point_id}", f"type={rec.fact_type}", f"sim={c.score:.3f}"]
        if rec.legacy:
            tags.append("legacy=1")
        lines.append(f"- [{', '.join(tags)}] {rec.fact}")
    return "\n".join(lines)


async def _arbitrate(fact: FactRecord, candidates) -> tuple[Decision, int, str]:
    """
    LLM 仲裁：返回 (decision, target_id, reason)。
    任何失败都退化为 ADD（宁可多存，不丢信息）。
    """
    new_line = f"[type={fact.fact_type}, confidence={fact.confidence:.2f}] {fact.fact}"
    cand_block = _render_candidate_block(candidates)

    try:
        prompt = _lp("nodes/fact_updater", new_fact_line=new_line, candidates_block=cand_block)
    except Exception as exc:
        logger.warning("加载 fact_updater prompt 失败，降级 ADD: %s", exc)
        return Decision.ADD, 0, "prompt load failed"

    try:
        llm = get_summary_llm()
        completion = await llm.ainvoke(
            [
                {"role": "system", "content": "你是严格按 JSON 输出的记忆协调者。"},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            timeout=45.0,
        )
        raw = (completion.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("fact_updater LLM 调用失败，降级 ADD: %s", exc)
        return Decision.ADD, 0, "llm call failed"

    parsed = _safe_parse(raw)
    if not parsed:
        logger.warning("fact_updater 返回不是合法 JSON，降级 ADD | raw=%.200s", raw)
        return Decision.ADD, 0, "json parse failed"

    raw_decision = str(parsed.get("decision") or "").upper().strip()
    try:
        decision = Decision(raw_decision)
    except ValueError:
        logger.warning("fact_updater 非法 decision=%r，降级 ADD", raw_decision)
        return Decision.ADD, 0, "invalid decision"

    try:
        target_id = int(parsed.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    reason = str(parsed.get("reason") or "")[:160]

    # 验证 target_id：必须指向候选列表中真实存在的 point_id，否则降级
    if decision in (Decision.UPDATE, Decision.DELETE):
        valid_ids = {c.point_id for c in candidates}
        if target_id not in valid_ids:
            logger.warning(
                "fact_updater target_id=%s 不在候选 %s 中，降级 ADD | decision=%s",
                target_id, valid_ids, decision,
            )
            return Decision.ADD, 0, f"target_id {target_id} invalid"

    return decision, target_id, reason


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _safe_parse(raw: str) -> Optional[dict]:
    text = raw.strip()
    if text.startswith("```"):
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
