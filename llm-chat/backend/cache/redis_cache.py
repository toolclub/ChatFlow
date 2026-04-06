"""
Redis Semantic Cache 后端

实现细节：
  - 使用 redis.asyncio（异步非阻塞）
  - RediSearch 向量索引（FLAT，适合中小规模缓存；量大时可改 HNSW）
  - Key 格式：cache:{namespace}:{md5_of_question}
  - 单个索引覆盖所有 cache: 前缀的 key，通过 TAG 字段 namespace 实现命名空间隔离
  - 向量：bge-m3 1024 维 FLOAT32，L2-normalize 后用 COSINE 距离
  - 相似度 = 1 - cosine_distance（Redis 返回的是距离，不是相似度）
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

import numpy as np
import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from cache.base import CacheLookupResult, SemanticCache
from llm.embeddings import embed_text

logger = logging.getLogger("cache.redis")

_KEY_PREFIX = "cache:"

# RediSearch TAG 字段中需要转义的特殊字符
_TAG_SPECIAL = re.compile(r'([,.<>{}"\'`:;!@#$%^&*()\-+=~|\\])')

# 连续失败超过此次数后触发自动修复
_HEAL_THRESHOLD = 3


class RedisCacheBackend(SemanticCache):
    """
    基于 RediSearch 的语义缓存后端。

    Args:
        redis_url:   Redis 连接串，如 redis://localhost:6379
        index_name:  RediSearch 索引名称（默认 semantic_cache）
        vector_dim:  Embedding 维度（需与 embed_text 模型一致）
        threshold:   相似度阈值（0-1），超过则判定为命中
    """

    def __init__(
        self,
        redis_url: str,
        index_name: str,
        vector_dim: int,
        threshold: float,
    ) -> None:
        self._redis_url  = redis_url
        self._index_name = index_name
        self._vector_dim = vector_dim
        self._threshold  = threshold
        self._client: Optional[Redis] = None
        self._error_count = 0   # 连续失败计数，超过阈值触发自动修复

    # ── 内部工具 ───────────────────────────────────────────────────────────────

    def _get_client(self) -> Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self._redis_url, decode_responses=False
            )
        return self._client

    @staticmethod
    def _build_key(namespace: str, question: str) -> str:
        """key 格式：cache:{namespace}:{md5}"""
        md5 = hashlib.md5(question.encode()).hexdigest()
        return f"{_KEY_PREFIX}{namespace}:{md5}"

    @staticmethod
    def _escape_tag(value: str) -> str:
        """
        转义 RediSearch TAG 查询中的特殊字符。
        RediSearch 要求 TAG filter 里的特殊字符（如 : - . 等）必须用反斜杠转义，
        否则会抛出 "Syntax error at offset N near X"。
        """
        return _TAG_SPECIAL.sub(r'\\\1', value)

    @staticmethod
    async def _vectorize(text: str) -> bytes:
        """向量化并 L2-normalize，返回 FLOAT32 bytes。"""
        vec = np.array(await embed_text(text), dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tobytes()

    async def _try_heal(self) -> None:
        """
        自动修复：删除损坏/不兼容的索引并重建。
        适用场景：索引 schema 不匹配、向量维度变更、索引内部损坏等。
        仅删除索引定义，不删除原始 Hash 数据（delete_docs=False）。
        """
        try:
            client = self._get_client()
            try:
                await client.ft(self._index_name).dropindex(delete_docs=False)
                logger.warning("Cache heal: 已删除损坏索引 '%s'，准备重建", self._index_name)
            except Exception as drop_err:
                logger.warning("Cache heal: 删除索引失败（忽略）: %s", drop_err)
            await self.init()
            logger.info("Cache heal: 索引 '%s' 重建完成", self._index_name)
        except Exception as exc:
            logger.error("Cache heal 失败，缓存将暂时不可用: %s", exc)

    # ── SemanticCache 接口实现 ─────────────────────────────────────────────────

    async def init(self) -> None:
        """创建 RediSearch 索引（已存在则跳过）。"""
        client = self._get_client()
        try:
            await client.ft(self._index_name).info()
            logger.info("Semantic cache: 索引 '%s' 已就绪", self._index_name)
        except Exception:
            schema = [
                TextField("question"),
                TextField("answer"),
                TagField("namespace"),
                VectorField(
                    "embedding",
                    "FLAT",
                    {
                        "TYPE": "FLOAT32",
                        "DIM": self._vector_dim,
                        "DISTANCE_METRIC": "COSINE",
                    },
                ),
            ]
            await client.ft(self._index_name).create_index(
                schema,
                definition=IndexDefinition(
                    prefix=[_KEY_PREFIX], index_type=IndexType.HASH
                ),
            )
            logger.info("Semantic cache: 索引 '%s' 创建完成", self._index_name)

    async def lookup(
        self,
        question: str,
        namespace: str = "global",
    ) -> Optional[CacheLookupResult]:
        """KNN 查询 + TAG 预过滤，未命中或低于阈值返回 None。"""
        try:
            q_vec = await self._vectorize(question)
            # TAG 预过滤 + KNN；namespace 中的特殊字符（: - 等）必须转义
            escaped_ns = self._escape_tag(namespace)
            raw_query = f"(@namespace:{{{escaped_ns}}})=>[KNN 1 @embedding $vec AS score]"
            query = (
                Query(raw_query)
                .sort_by("score")
                .return_fields("question", "answer", "score")
                .dialect(2)
            )
            result = await self._get_client().ft(self._index_name).search(
                query, query_params={"vec": q_vec}
            )
            # 成功：重置连续失败计数
            self._error_count = 0

            if result.total == 0:
                return None

            doc = result.docs[0]
            similarity = 1.0 - float(doc.score)
            if similarity < self._threshold:
                logger.debug(
                    "Cache MISS  similarity=%.4f < threshold=%.2f  ns=%s",
                    similarity, self._threshold, namespace,
                )
                return None

            decode = lambda v: v.decode() if isinstance(v, bytes) else v
            answer  = decode(doc.answer)
            matched = decode(doc.question)
            logger.info(
                "Cache HIT   similarity=%.4f  ns=%s  matched='%.50s'",
                similarity, namespace, matched,
            )
            return CacheLookupResult(
                answer=answer,
                matched_question=matched,
                similarity=similarity,
                namespace=namespace,
            )
        except Exception as exc:
            self._error_count += 1
            logger.error(
                "Cache lookup 失败 (%d/%d): %s",
                self._error_count, _HEAL_THRESHOLD, exc,
            )
            if self._error_count >= _HEAL_THRESHOLD:
                logger.warning("Cache: 连续失败 %d 次，触发自动修复", self._error_count)
                self._error_count = 0
                await self._try_heal()
            return None

    async def store(
        self,
        question: str,
        answer: str,
        namespace: str = "global",
        ttl_seconds: int | None = None,
    ) -> None:
        """向量化 question 后写入 Redis Hash，可选 TTL。"""
        try:
            key = self._build_key(namespace, question)
            embedding = await self._vectorize(question)
            client = self._get_client()
            await client.hset(
                key,
                mapping={
                    "question":  question,
                    "answer":    answer,
                    "namespace": namespace,
                    "embedding": embedding,
                },
            )
            if ttl_seconds:
                await client.expire(key, ttl_seconds)
            logger.debug(
                "Cache STORE  ns=%s  ttl=%s  question='%.50s'",
                namespace, f"{ttl_seconds}s" if ttl_seconds else "永不过期", question,
            )
        except Exception as exc:
            self._error_count += 1
            logger.error(
                "Cache store 失败 (%d/%d): %s",
                self._error_count, _HEAL_THRESHOLD, exc,
            )
            if self._error_count >= _HEAL_THRESHOLD:
                logger.warning("Cache: 连续失败 %d 次，触发自动修复", self._error_count)
                self._error_count = 0
                await self._try_heal()

    async def clear(self, namespace: Optional[str] = None) -> None:
        """用 SCAN 逐批删除匹配的 key，避免大规模操作阻塞 Redis。"""
        pattern = (
            f"{_KEY_PREFIX}{namespace}:*"
            if namespace
            else f"{_KEY_PREFIX}*"
        )
        client = self._get_client()
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await client.scan(cursor, match=pattern, count=100)
            if keys:
                await client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        logger.info("Cache clear  pattern=%s  deleted=%d", pattern, deleted)
