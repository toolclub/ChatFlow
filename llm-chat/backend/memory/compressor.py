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

from config import (
    FACT_EXTRACTION_ENABLED,
    LONGTERM_MEMORY_ENABLED,
    SUMMARY_SYSTEM_PROMPT,
)
from prompts import load_prompt as _lp

_compressor_instruction = _lp("nodes/compressor")
from llm.chat import get_summary_llm
from memory import store as memory_store
from memory.context_builder import should_compress, slice_for_compression

logger = logging.getLogger("memory.compressor")

_TOOL_SUMMARY_MARKER = "\n\n【工具调用记录】"


def _strip_tool_summary(content: str) -> str:
    # COMPAT: 兼容旧数据 — 旧消息的工具调用记录可能仍嵌入在 content 中。
    # 新消息已将 tool_summary 存入独立字段，此函数仅处理迁移前的历史数据。
    # 当所有旧消息完成压缩后可移除此函数。
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

    # 先写入长期记忆（Qdrant）— embedding 失败时跳过，不影响压缩主流程
    # 新架构（FACT_EXTRACTION_ENABLED）下事实已在每轮 extract_memory 节点写入，
    # 这里不再重复批量写 Q&A 对，避免长期记忆表噪音过多。
    if LONGTERM_MEMORY_ENABLED and not FACT_EXTRACTION_ENABLED:
        try:
            from rag.ingestor import batch_store_pairs
            await batch_store_pairs(
                conv_id, to_summarise, conv.mid_term_cursor, user_id=conv.client_id,
            )
        except Exception as exc:
            logger.warning("长期记忆写入失败（已跳过，不影响压缩）| conv=%s | error=%s", conv_id, exc)

    # 构建摘要提示（工具调用记录不混入，避免摘要模型处理大量噪音）
    def _content_for_summary(m: "Message") -> str:
        """获取用于摘要的消息内容：新数据直接用 content，旧数据兼容 _strip_tool_summary。"""
        if m.tool_summary:
            # 新数据：tool_summary 已在独立字段，content 是纯文本
            return m.content
        # COMPAT: 旧数据可能在 content 中嵌入了工具调用记录
        return _strip_tool_summary(m.content)

    history_text = "\n".join(
        f"{'用户' if m.role == 'user' else 'AI'}: {_content_for_summary(m)}"
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

    # 将已压缩消息中的工具调用记录清除（减少后续上下文窗口噪音）
    for msg in to_summarise:
        if msg.role == "assistant":
            # 新数据：清空独立字段
            had_summaries = bool(msg.tool_summary or msg.step_summary)
            if had_summaries:
                msg.tool_summary = ""
                msg.step_summary = ""
                await memory_store.clear_message_summaries(msg.id)
            # COMPAT: 旧数据可能在 content 中嵌入了工具调用记录
            if _TOOL_SUMMARY_MARKER in msg.content:
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
