"""
Layer 6 – Context
Assembles the message list sent to the LLM; decides when compression is needed.
Progressive disclosure: system → mid-term summary → sliding window of recent messages.

Full message history is NEVER deleted. Compression only advances mid_term_cursor
and updates the rolling summary.
"""
from config import SHORT_TERM_MAX_TURNS, COMPRESS_TRIGGER
from layers.memory import Conversation, Message


def build_messages(conv: Conversation) -> list[dict]:
    """
    Build the ordered message list for the LLM:
      1. System prompt          (Layer 1 – Prompt)
      2. Mid-term summary       (Layer 3 – Memory: semantic)
      3. [reserved] RAG results (Layer 3 – Memory: long-term)
      4. Sliding window of recent messages (Layer 3 – Memory: episodic)
    """
    result: list[dict] = []

    # 1. System prompt
    result.append({"role": "system", "content": conv.system_prompt})

    # 2. Mid-term summary (semantic memory)
    if conv.mid_term_summary:
        result.append({
            "role": "system",
            "content": (
                "【对话背景摘要】以下是之前对话的压缩摘要，请结合这些背景来回答：\n"
                f"{conv.mid_term_summary}"
            ),
        })

    # 3. Reserved: long-term RAG injection point
    # if conv.rag_enabled:
    #     docs = await rag_search(user_query, conv.long_term_collection)
    #     result.append({"role": "system", "content": f"【相关知识】\n{docs}"})

    # 4. Sliding window – most recent N turns from full history
    window = conv.messages[-(SHORT_TERM_MAX_TURNS * 2):]
    for msg in window:
        result.append({"role": msg.role, "content": msg.content})

    return result


def should_compress(conv: Conversation) -> bool:
    """True when there are enough unsummarised messages to warrant compression."""
    unsummarised = len(conv.messages) - conv.mid_term_cursor
    return unsummarised >= COMPRESS_TRIGGER * 2


def slice_for_compression(conv: Conversation) -> tuple[list[Message], int]:
    """
    Return (messages_to_summarise, new_cursor).
    Only the messages between the current cursor and the start of the
    sliding window are sent to the summary model – keeping the window intact.
    Messages are NEVER removed from conv.messages.
    """
    keep_count = (SHORT_TERM_MAX_TURNS // 2) * 2
    new_cursor = max(conv.mid_term_cursor, len(conv.messages) - keep_count)
    to_summarise = conv.messages[conv.mid_term_cursor:new_cursor]
    return to_summarise, new_cursor
