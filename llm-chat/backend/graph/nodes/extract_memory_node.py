"""
ExtractMemoryNode：在每轮对话结束后，把本轮 user/assistant 压成"事实"写入长期记忆。

位置：save_response → extract_memory → compress_memory → END
   （compress_memory 之前触发，保证 extractor 看到的还是未被压缩清理的 tool_summary）

执行模式：
    fire-and-forget（asyncio.create_task + 强引用集合），
    不阻塞主图返回，不影响用户感知延迟。
    任何异常只 log warning（spec 铁律 #9），不向上传播。

触发条件：
    * FACT_EXTRACTION_ENABLED = True
    * LONGTERM_MEMORY_ENABLED = True
    * 本轮不是澄清轮（needs_clarification=True 时跳过）
    * 本轮有完整 user 消息 + 助手回复（空 response 不抽取）

spec 铁律说明：
    * #1 不推断状态：本节点只读 state（user_message / full_response / conv_id / client_id）
    * #6 流式：extractor/updater 是后台 Task，无 SSE，使用 ainvoke（与 compressor 同级）
    * #9 异常：全部 logger.warning 兜底
"""
from __future__ import annotations

import asyncio
import logging

from config import FACT_EXTRACTION_ENABLED, LONGTERM_MEMORY_ENABLED
from graph.nodes.base import BaseNode
from graph.state import GraphState
from memory import store as memory_store

logger = logging.getLogger("graph.nodes.extract_memory")

# fire-and-forget Task 的强引用集合，防止 GC 中途回收
# 模块级单例，跨对话共享；Task done 后自动 discard。
_pending: "set[asyncio.Task]" = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


class ExtractMemoryNode(BaseNode):
    """每轮结束后抽取事实，fire-and-forget 写入 Qdrant。"""

    @property
    def name(self) -> str:
        return "extract_memory"

    async def execute(self, state: GraphState) -> dict:
        if not (FACT_EXTRACTION_ENABLED and LONGTERM_MEMORY_ENABLED):
            return {}

        # 澄清轮不抽取：这一轮助手输出的是澄清卡片，不是对用户的正面回答
        if state.get("needs_clarification"):
            return {}

        conv_id       = state.get("conv_id", "")
        user_msg      = (state.get("user_message") or "").strip()
        full_response = (state.get("full_response") or "").strip()
        if not (conv_id and user_msg and full_response):
            return {}

        conv = memory_store.get(conv_id)
        if conv is None:
            logger.debug("extract_memory 跳过：conv %s 不在缓存中", conv_id)
            return {}

        user_id = state.get("client_id", "") or conv.client_id or ""

        # 工具摘要 / 步骤摘要：给 extractor 更丰富的上下文，帮助它区分"搜索结果"和"事实"
        tool_summary = ""
        # state 中并未直接提供 tool_summary，只能从 save_response_node 构建的
        # _build_tool_summary 复现；这里保持轻量，直接从 messages 内提取。
        try:
            tool_summary = self._quick_tool_summary(state)
        except Exception as exc:
            logger.debug("quick_tool_summary 失败（忽略）: %s", exc)

        # 源消息 ID：assistant 消息的 DB id（pre_assistant_db_id 预写时生成）
        source_msg_id = int(state.get("pre_user_db_id") or 0)

        core_memory_snapshot = dict(conv.core_memory or {})

        _spawn(_run_extraction(
            conv_id=conv_id,
            user_id=user_id,
            user_msg=user_msg,
            assistant_msg=full_response,
            tool_summary=tool_summary,
            source_msg_id=source_msg_id,
            core_memory=core_memory_snapshot,
        ))
        return {}

    @staticmethod
    def _quick_tool_summary(state: GraphState) -> str:
        """
        轻量版工具摘要：遍历 state.messages，取工具调用名 + 结果前 200 字。
        目的是让 extractor 知道本轮调用了什么工具、返回了什么关键信息。
        """
        messages = list(state.get("messages") or [])
        lines: list[str] = []
        for m in messages:
            cls = type(m).__name__
            if cls == "AIMessage":
                tcs = getattr(m, "tool_calls", None) or []
                for tc in tcs:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name:
                        lines.append(f"调用: {name}")
            elif cls == "ToolMessage":
                text = str(getattr(m, "content", ""))[:200]
                if text:
                    lines.append(f"结果: {text}")
        if not lines:
            return ""
        return "\n".join(lines[:12])


async def _run_extraction(
    *,
    conv_id: str,
    user_id: str,
    user_msg: str,
    assistant_msg: str,
    tool_summary: str,
    source_msg_id: int,
    core_memory: dict,
) -> None:
    """抽取 + 逐条 ingest，所有异常只 log，不传播。"""
    try:
        from rag.extractor import ExtractionRequest, extract_facts_from_turn
        from rag.updater import ingest_fact

        facts = await extract_facts_from_turn(ExtractionRequest(
            conv_id=conv_id,
            user_id=user_id,
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            tool_summary=tool_summary,
            source_msg_id=source_msg_id,
            core_memory=core_memory,
        ))
        if not facts:
            logger.info("extract_memory: 本轮无事实可抽取 | conv=%s", conv_id)
            return

        for fact in facts:
            try:
                await ingest_fact(fact)
            except Exception as exc:
                logger.warning(
                    "ingest_fact 失败（继续下一条）| conv=%s | fact=%.80s | error=%s",
                    conv_id, fact.fact, exc,
                )
    except asyncio.CancelledError:
        logger.info("extract_memory 被取消 | conv=%s", conv_id)
        raise
    except Exception as exc:
        logger.warning("extract_memory 后台异常 | conv=%s | error=%s", conv_id, exc)
