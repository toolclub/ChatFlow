"""
Layer 3b – Long-term Memory
Qdrant 向量存储：把每轮 Q&A 对嵌入后持久化，在下一次对话前检索最相关的历史记忆注入上下文。

存储单元：一对 (user_msg, assistant_msg) 作为一个 Point，向量取自 user_msg（用于相关性匹配）。
检索策略：用当前用户问题做 Embedding，按余弦相似度取 TOP-K，过滤同一会话。
author: leizihao
email: lzh19162600626@gmail.com
"""
import hashlib
import logging
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
    EMBEDDING_MODEL,
    LONGTERM_SCORE_THRESHOLD,
    LONGTERM_TOP_K,
    QDRANT_COLLECTION,
    QDRANT_HOST,
    QDRANT_PORT,
)
from ollama_client import get_embedding as _embed

logger = logging.getLogger("longterm")

_client: Optional[AsyncQdrantClient] = None


def get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    return _client


def _point_id(conv_id: str, user_idx: int) -> int:
    """稳定可重现的 uint63 point ID，避免 hash() 受 PYTHONHASHSEED 影响。"""
    raw = f"{conv_id}:{user_idx}".encode()
    digest = hashlib.md5(raw).digest()
    return int.from_bytes(digest[:8], "big") % (2 ** 63)


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


async def store_pair(
    conv_id: str,
    user_msg: str,
    assistant_msg: str,
    user_idx: int,
) -> None:
    """
    将一轮对话 (user_msg, assistant_msg) 向量化后存入 Qdrant。
    向量来自 user_msg，便于按"问题相关性"检索。
    """
    try:
        vector = await _embed(user_msg, EMBEDDING_MODEL)
        point_id = _point_id(conv_id, user_idx)
        await get_client().upsert(
            collection_name=QDRANT_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "conv_id": conv_id,
                        "user": user_msg,
                        "assistant": assistant_msg,
                        "msg_idx": user_idx,
                    },
                )
            ],
        )
        logger.debug("Qdrant: 已存储 conv=%s idx=%d", conv_id, user_idx)
    except Exception as exc:
        logger.error("Qdrant store_pair 失败: %s", exc)


async def search_memories(
    conv_id: str,
    query: str,
    top_k: int = LONGTERM_TOP_K,
) -> list[str]:
    """
    检索与 query 最相关的历史 Q&A 对。
    返回格式：["用户: ...\n助手: ...", ...]
    """
    try:
        vector = await _embed(query, EMBEDDING_MODEL)
        results = await get_client().search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vector,
            query_filter=Filter(
                must=[FieldCondition(key="conv_id", match=MatchValue(value=conv_id))]
            ),
            limit=top_k,
            score_threshold=LONGTERM_SCORE_THRESHOLD,
        )
        memories = []
        for r in results:
            user = r.payload.get("user", "")
            assistant = r.payload.get("assistant", "")
            memories.append(f"用户: {user}\n助手: {assistant}")
        return memories
    except Exception as exc:
        logger.error("Qdrant search_memories 失败: %s", exc)
        return []


async def delete_by_conv(conv_id: str) -> None:
    """删除某会话的所有长期记忆 Point。"""
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
        logger.error("Qdrant delete_by_conv 失败: %s", exc)


async def count_by_conv(conv_id: str) -> int:
    """返回某会话在 Qdrant 中存储的记忆条数（用于调试接口）。"""
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
        logger.error("Qdrant count_by_conv 失败: %s", exc)
        return -1
