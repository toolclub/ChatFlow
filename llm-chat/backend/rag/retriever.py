"""
RAG 检索层：Qdrant 向量存储（事实级）

存储粒度：
    一条"事实" = 一个 Qdrant point，payload 由 `rag.fact_schema.FactRecord` 定义。
    旧版一条 Q&A 对 = 一个 point，payload = {user, assistant, msg_idx}。
    本模块同时兼容两种 payload（COMPAT），`FactRecord.from_payload` 统一还原。

检索策略：
    * 默认按 `user_id`（= Conversation.client_id）过滤，允许跨对话复用记忆
    * user_id 缺失（旧数据 / 匿名）时退回按 `conv_id` 过滤，避免污染他人
    * 余弦相似度 Top-K，低于 LONGTERM_SCORE_THRESHOLD 的丢弃
    * 过滤掉已被 superseded_by 标记的旧版本事实

写入策略：
    * 新路径：`rag.updater.ingest_fact` 逐条写入（save_response 后的 extract_memory 节点）
    * 旧路径：`rag.ingestor.batch_store_pairs` 在压缩时批量写（COMPAT，逐步淘汰）

忘记模式：
    RAG 未命中时，计算 query 与摘要/近期消息的余弦相似度判断话题连续性
    （保持与旧实现一致，供 retrieve_context_node 调用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from config import (
    EMBEDDING_DIM,
    LONGTERM_SCORE_THRESHOLD,
    LONGTERM_TOP_K,
    QDRANT_COLLECTION,
    QDRANT_URL,
    SUMMARY_RELEVANCE_THRESHOLD,
)
from llm.embeddings import embed_text
from rag.fact_schema import FactRecord, FACT_TYPE_LEGACY_PAIR

logger = logging.getLogger("rag.retriever")

_client: Optional[AsyncQdrantClient] = None


def get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=QDRANT_URL)
    return _client


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _legacy_point_id(conv_id: str, user_idx: int) -> int:
    """旧版稳定 ID（供 ingestor 复用时保持幂等）。"""
    raw = f"{conv_id}:{user_idx}".encode()
    digest = hashlib.md5(raw).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)


def new_fact_point_id(user_id: str, fact: str) -> int:
    """
    事实 point_id 生成器：基于 (user_id, fact 文本, 时间戳) 生成稳定 uint63。
    同一条 fact 文本重复写入时因时间戳不同仍会分配新 id；去重由 updater 层负责。
    """
    seed = f"{user_id}::{fact}::{time.time_ns()}".encode("utf-8")
    digest = hashlib.md5(seed).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _user_or_conv_filter(user_id: str, conv_id: str) -> Filter:
    """
    user_id 非空时按 user_id 过滤（跨对话复用记忆）。
    否则按 conv_id 过滤（兼容旧数据 / 匿名会话，防止污染他人）。
    """
    if user_id:
        return Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
    return Filter(must=[FieldCondition(key="conv_id", match=MatchValue(value=conv_id))])


# ── 初始化 ────────────────────────────────────────────────────────────────────

async def init_collection() -> None:
    """启动时调用：若 Collection 不存在则创建。"""
    client = get_client()
    resp = await client.get_collections()
    existing = {c.name for c in resp.collections}
    if QDRANT_COLLECTION not in existing:
        await client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        logger.info("Qdrant: 已创建 Collection '%s'", QDRANT_COLLECTION)
    else:
        logger.info("Qdrant: Collection '%s' 就绪", QDRANT_COLLECTION)


# ── 新路径：事实级写入 / 查询 / 删除（供 updater 调用） ────────────────────────

@dataclass
class FactHit:
    """检索命中的事实（含 Qdrant point_id 和分数）。"""
    point_id: int
    score: float
    record: FactRecord


async def upsert_fact(record: FactRecord) -> int:
    """
    写入一条事实，返回分配的 point_id。
    embedding 的源文本 = record.fact（事实本身），保证检索命中语义相同的事实。
    """
    try:
        vector = await embed_text(record.fact)
    except Exception as exc:
        logger.warning("upsert_fact embedding 失败: %s", exc)
        return 0

    point_id = new_fact_point_id(record.user_id or record.conv_id, record.fact)
    try:
        await get_client().upsert(
            collection_name=QDRANT_COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=record.to_payload())],
        )
        logger.info(
            "Qdrant upsert_fact | user=%s | conv=%s | type=%s | id=%d | fact=%.80s",
            record.user_id, record.conv_id, record.fact_type, point_id, record.fact,
        )
        return point_id
    except Exception as exc:
        logger.warning("Qdrant upsert_fact 失败: %s", exc)
        return 0


async def delete_point(point_id: int) -> bool:
    """按 point_id 删除一条事实。"""
    try:
        await get_client().delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=[point_id],
        )
        return True
    except Exception as exc:
        logger.warning("Qdrant delete_point(%s) 失败: %s", point_id, exc)
        return False


async def mark_superseded(old_point_id: int, new_point_id: int) -> bool:
    """
    在旧事实 payload 上写入 superseded_by 字段，保留追溯但检索时会被过滤。
    使用 set_payload（局部更新，不覆盖整张 payload）。
    """
    try:
        await get_client().set_payload(
            collection_name=QDRANT_COLLECTION,
            payload={"superseded_by": int(new_point_id), "superseded_ts": time.time()},
            points=[old_point_id],
        )
        return True
    except Exception as exc:
        logger.warning("Qdrant mark_superseded(%s) 失败: %s", old_point_id, exc)
        return False


async def search_fact_candidates(
    user_id: str,
    conv_id: str,
    query: str,
    top_k: int,
    score_threshold: float,
) -> list[FactHit]:
    """
    供 updater 判断冲突用的召回：不过滤 superseded_by（需要看到旧版本），
    不过滤 legacy（需要纳入旧数据做新旧合并）。
    """
    try:
        vector = await embed_text(query)
    except Exception as exc:
        logger.warning("search_fact_candidates embedding 失败: %s", exc)
        return []

    try:
        response = await get_client().query_points(
            collection_name=QDRANT_COLLECTION,
            query=vector,
            query_filter=_user_or_conv_filter(user_id, conv_id),
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
    except Exception as exc:
        logger.warning("search_fact_candidates 查询失败: %s", exc)
        return []

    hits: list[FactHit] = []
    for p in response.points:
        rec = FactRecord.from_payload(p.payload)
        if rec is None:
            continue
        hits.append(FactHit(point_id=int(p.id), score=float(p.score), record=rec))
    return hits


# ── 主检索入口（供 retrieve_context_node 调用） ───────────────────────────────

async def search_memories(
    conv_id: str,
    query: str,
    top_k: int = LONGTERM_TOP_K,
    user_id: str = "",
) -> list[str]:
    """
    检索与 query 最相关的长期记忆，返回可直接注入系统提示的字符串列表。

    过滤规则：
      - 已被 superseded_by 标记的事实跳过（旧版本，有更新）
      - 新路径 fact 直接取 record.fact
      - COMPAT 旧路径 Q&A 对 渲染成 "用户: ... / 助手: ..." 形式

    Args:
        conv_id:  当前对话 ID（fallback 过滤键）
        query:    本轮用户消息
        top_k:    返回条数上限
        user_id:  用户标识（= client_id），非空时按 user 过滤；
                  空时退化为按 conv_id 过滤，避免匿名情况下跨用户串号
    """
    try:
        vector = await embed_text(query)
    except Exception as exc:
        logger.warning("search_memories embedding 失败: %s", exc)
        return []

    try:
        response = await get_client().query_points(
            collection_name=QDRANT_COLLECTION,
            query=vector,
            query_filter=_user_or_conv_filter(user_id, conv_id),
            limit=top_k * 2,  # 多取一些，后续过滤 superseded
            score_threshold=LONGTERM_SCORE_THRESHOLD,
            with_payload=True,
        )
    except Exception as exc:
        logger.warning("search_memories 查询失败: %s", exc)
        return []

    rendered: list[str] = []
    for p in response.points:
        payload = p.payload or {}
        # 跳过已失效
        if payload.get("superseded_by"):
            continue
        rec = FactRecord.from_payload(payload)
        if rec is None:
            continue
        line = rec.render_for_context().strip()
        if line:
            rendered.append(line)
        if len(rendered) >= top_k:
            break
    return rendered


# ── 忘记模式判断（与旧版保持一致） ────────────────────────────────────────────

async def is_relevant_to_summary(query: str, summary: str) -> bool:
    """判断 query 是否与中期摘要在语义上相关（低于阈值则触发忘记模式）。"""
    try:
        vec_q, vec_s = await asyncio.gather(embed_text(query), embed_text(summary))
        sim = _cosine_similarity(vec_q, vec_s)
        logger.info("query与摘要相似度: %.4f (阈值: %.2f)", sim, SUMMARY_RELEVANCE_THRESHOLD)
        return sim >= SUMMARY_RELEVANCE_THRESHOLD
    except Exception as exc:
        logger.warning("is_relevant_to_summary 失败: %s", exc)
        return True  # 出错保守处理，不触发忘记


async def is_relevant_to_recent(query: str, recent_msgs: list[str]) -> bool:
    """无摘要时的替代方案：与最近几条用户消息的平均余弦相似度判断话题连续性。"""
    try:
        vecs = await asyncio.gather(
            embed_text(query), *[embed_text(m) for m in recent_msgs]
        )
        sims = [_cosine_similarity(vecs[0], v) for v in vecs[1:]]
        avg_sim = sum(sims) / len(sims) if sims else 0.0
        logger.info("query与近期消息平均相似度: %.4f (阈值: %.2f)", avg_sim, SUMMARY_RELEVANCE_THRESHOLD)
        return avg_sim >= SUMMARY_RELEVANCE_THRESHOLD
    except Exception as exc:
        logger.warning("is_relevant_to_recent 失败: %s", exc)
        return True


# ── COMPAT：旧版 Q&A 对写入接口（供 ingestor.batch_store_pairs 复用） ──────────

async def store_pair(
    conv_id: str,
    user_msg: str,
    assistant_msg: str,
    user_idx: int,
    user_id: str = "",
) -> None:
    """
    COMPAT：旧版每轮 Q&A 对写入。新版已改用 extractor + updater 管线。
    保留此函数是为了：
      1. compressor.batch_store_pairs 在 fact_extraction_enabled=false 时仍能工作
      2. 数据迁移脚本可复用

    user_id 参数新加：旧版未存；写入时补上以便跨会话检索。
    """
    try:
        vector = await embed_text(user_msg)
        point_id = _legacy_point_id(conv_id, user_idx)
        payload: dict = {
            "schema":     "qa_pair_v0",  # 显式标注旧 schema，方便后续迁移识别
            "conv_id":    conv_id,
            "user":       user_msg[:2000],
            "assistant":  assistant_msg[:2000],
            "msg_idx":    user_idx,
        }
        if user_id:
            payload["user_id"] = user_id
        await get_client().upsert(
            collection_name=QDRANT_COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        logger.debug("Qdrant store_pair (legacy) | conv=%s | idx=%d", conv_id, user_idx)
    except Exception as exc:
        logger.warning("Qdrant store_pair 失败: %s", exc)


# ── 删除 / 统计 ───────────────────────────────────────────────────────────────

async def delete_by_conv(conv_id: str) -> None:
    """删除某会话的所有长期记忆（按 conv_id 过滤，新旧 schema 都会被清理）。"""
    try:
        await get_client().delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="conv_id", match=MatchValue(value=conv_id))]
                )
            ),
        )
        logger.info("Qdrant: 已清除 conv=%s 的所有记忆", conv_id)
    except Exception as exc:
        logger.warning("Qdrant delete_by_conv 失败: %s", exc)


async def delete_by_user(user_id: str) -> None:
    """删除某用户（client_id）的所有长期记忆（用户注销 / 数据清理用）。"""
    if not user_id:
        return
    try:
        await get_client().delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
                )
            ),
        )
        logger.info("Qdrant: 已清除 user=%s 的所有记忆", user_id)
    except Exception as exc:
        logger.warning("Qdrant delete_by_user 失败: %s", exc)


async def count_by_conv(conv_id: str) -> int:
    """返回某会话在 Qdrant 中存储的记忆条数（供调试接口使用）。"""
    try:
        result = await get_client().count(
            collection_name=QDRANT_COLLECTION,
            count_filter=Filter(
                must=[FieldCondition(key="conv_id", match=MatchValue(value=conv_id))]
            ),
            exact=True,
        )
        return result.count
    except Exception as exc:
        logger.warning("Qdrant count_by_conv 失败: %s", exc)
        return -1


async def count_by_user(user_id: str) -> int:
    """返回某用户在 Qdrant 中存储的记忆条数（跨对话总量）。"""
    if not user_id:
        return 0
    try:
        result = await get_client().count(
            collection_name=QDRANT_COLLECTION,
            count_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            exact=True,
        )
        return result.count
    except Exception as exc:
        logger.warning("Qdrant count_by_user 失败: %s", exc)
        return -1
