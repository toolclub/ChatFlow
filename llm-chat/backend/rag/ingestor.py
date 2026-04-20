"""
RAG 写入层（COMPAT）：压缩时批量把 Q&A 对写入 Qdrant。

与 retriever 分开，保持职责清晰。

说明：
    * 本函数是旧架构的批量写入路径。
    * 新架构走 extractor → updater 的事实级管线（见 graph.nodes.extract_memory_node）。
    * 当 `FACT_EXTRACTION_ENABLED=True`（默认）时，compressor 将跳过本函数，
      因为事实已在每轮实时抽取。关闭事实抽取（回退到旧版行为）时仍会调用。
"""
import logging

from memory.schema import Message
from rag import retriever

logger = logging.getLogger("rag.ingestor")


async def batch_store_pairs(
    conv_id: str,
    messages: list[Message],
    base_idx: int,
    user_id: str = "",
) -> None:
    """
    将待压缩的消息列表中的 user/assistant 对批量写入 Qdrant（旧版 Q&A 格式）。

    Args:
        conv_id:   对话 ID
        messages:  待摘要的消息列表（从游标到滑动窗口起点）
        base_idx:  messages[0] 在完整历史中的索引（用于生成稳定 point_id）
        user_id:   用户标识（= Conversation.client_id），写入 payload 便于跨会话检索
    """
    i = 0
    stored = 0
    while i + 1 < len(messages):
        if messages[i].role == "user" and messages[i + 1].role == "assistant":
            await retriever.store_pair(
                conv_id,
                messages[i].content,
                messages[i + 1].content,
                base_idx + i,
                user_id=user_id,
            )
            stored += 1
        i += 2
    logger.info("RAG 批量写入完成: conv=%s user_id=%s 写入 %d 对", conv_id, user_id, stored)
