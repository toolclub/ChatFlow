"""
上下文压缩器：对话过长时生成滚动摘要，推进游标

压缩流程：
  1. should_compress() 检查阈值
  2. slice_for_compression() 确定待摘要范围
  3. batch_store_pairs() 将待摘要消息写入 Qdrant（RAG 长期记忆）
  4. 调用摘要模型生成新摘要（工具调用记录替换为 [old tools call]）
  5. 更新 conv.mid_term_summary + mid_term_cursor
  6. 将已压缩消息中的工具调用记录替换为 [old tools call] 占位符并持久化

已迁移：使用原生 openai LLMClient，不再依赖 langchain_openai。
"""
import logging

from config import LONGTERM_MEMORY_ENABLED, SUMMARY_SYSTEM_PROMPT
from prompts import load_prompt as _lp

_compressor_instruction = _lp("nodes/compressor")
from llm.chat import get_summary_llm
from memory import store as memory_store
from memory.context_builder import should_compress, slice_for_compression

logger = logging.getLogger("memory.compressor")

_TOOL_SUMMARY_MARKER = "\n\n【工具调用记录】"


def _strip_tool_summary(content: str) -> str:
    """将工具调用记录段落替换为 [old tools call] 占位符。"""
    idx = content.find(_TOOL_SUMMARY_MARKER)
    if idx >= 0:
        return content[:idx] + "\n\n[old tools call]"
    return content


async def maybe_compress(conv_id: str) -> bool:
    """
    检查并按需执行压缩。

    Returns:
        True 表示执行了压缩，False 表示未执行。
    """
    conv = memory_store.get(conv_id)
    if not conv or not should_compress(conv):
        return False

    to_summarise, new_cursor = slice_for_compression(conv)
    if not to_summarise:
        return False

    # 先写入长期记忆（Qdrant）
    if LONGTERM_MEMORY_ENABLED:
        from rag.ingestor import batch_store_pairs
        await batch_store_pairs(conv_id, to_summarise, conv.mid_term_cursor)

    # 构建摘要提示（工具调用记录用占位符，避免摘要模型处理大量噪音）
    history_text = "\n".join(
        f"{'用户' if m.role == 'user' else 'AI'}: {_strip_tool_summary(m.content)}"
        for m in to_summarise
    )
    existing = conv.mid_term_summary
    prompt_content = (
        (f"已有摘要：\n{existing}\n\n" if existing else "")
        + f"新增对话：\n{history_text}\n\n"
        + _compressor_instruction
    )

    # 使用原生 OpenAI LLMClient（替代旧版 langchain ChatOpenAI.ainvoke）
    llm = get_summary_llm()
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": prompt_content},
    ]
    completion = await llm.ainvoke(messages)
    new_summary = (completion.choices[0].message.content or "").strip()

    conv.mid_term_summary = new_summary
    conv.mid_term_cursor = new_cursor
    await memory_store.save(conv)

    # 将已压缩消息中的工具调用段落更新为占位符（减少后续上下文窗口噪音）
    for msg in to_summarise:
        if msg.role == "assistant" and _TOOL_SUMMARY_MARKER in msg.content:
            new_content = _strip_tool_summary(msg.content)
            msg.content = new_content
            await memory_store.update_message_content(msg.id, new_content)

    logger.info(
        "压缩完成 conv=%s 摘要了%d条消息，窗口=%d，摘要长度=%d",
        conv_id,
        len(to_summarise),
        len(conv.messages) - new_cursor,
        len(new_summary),
    )
    return True
