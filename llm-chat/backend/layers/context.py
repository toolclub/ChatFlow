"""
第 6 层 – Context（上下文）
负责组装发送给 LLM 的消息列表，并决定何时需要触发压缩。
渐进式披露顺序：系统提示 → 中期摘要 → 最近消息的滑动窗口。

完整消息历史永远不会被删除。压缩操作仅推进 mid_term_cursor
并更新滚动摘要。
author: leizihao
email: lzh19162600626@gmail.com
"""
from config import SHORT_TERM_MAX_TURNS, COMPRESS_TRIGGER
from layers.memory import Conversation, Message


def build_messages(
    conv: Conversation,
    long_term_memories: list[str] | None = None,
) -> list[dict]:
    """
    按顺序构建发送给 LLM 的消息列表：
      1. 系统提示               （第 1 层 – Prompt）
      2. 中期摘要               （第 3 层 – Memory：语义记忆）
      3. 长期记忆               （第 3b 层 – Memory：向量检索）
      4. 最近消息的滑动窗口     （第 3 层 – Memory：情节记忆）
    """
    # 1. 系统提示
    result: list[dict] = [{"role": "system", "content": conv.system_prompt}]


    # 2. 中期摘要（语义记忆）
    if conv.mid_term_summary:
        result.append({
            "role": "system",
            "content": (
                "【对话背景摘要】以下是之前对话的压缩摘要，请结合这些背景来回答：\n"
                f"{conv.mid_term_summary}"
            ),
        })

    # 3. 注入长期记忆（Qdrant 向量检索）
    if long_term_memories:
        memories_text = "\n\n".join(long_term_memories)
        result.append({
            "role": "system",
            "content": (
                "【长期记忆】以下是与当前问题高度相关的历史对话记录，请参考这些内容来回答：\n"
                f"{memories_text}"
            ),
        })

    # 4. 滑动窗口——从完整历史中取最近 N 轮
    window = conv.messages[-(SHORT_TERM_MAX_TURNS * 2):]
    for msg in window:
        result.append({"role": msg.role, "content": msg.content})

    return result


def should_compress(conv: Conversation) -> bool:
    """当未摘要的消息数量足够多、值得触发压缩时返回 True。"""
    unsummarised = len(conv.messages) - conv.mid_term_cursor
    return unsummarised >= COMPRESS_TRIGGER * 2


def slice_for_compression(conv: Conversation) -> tuple[list[Message], int]:
    """
    返回 (待摘要消息列表, 新游标位置)。
    只将当前游标到滑动窗口起点之间的消息发送给摘要模型——保持滑动窗口完整。
    conv.messages 中的消息永远不会被删除。
    """
    keep_count = (SHORT_TERM_MAX_TURNS // 2) * 2
    new_cursor = max(conv.mid_term_cursor, len(conv.messages) - keep_count)
    to_summarise = conv.messages[conv.mid_term_cursor:new_cursor]
    return to_summarise, new_cursor
