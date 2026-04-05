"""
CallModelAfterToolNode：工具执行后的 LLM 综合节点

职责：
  - 在工具执行完成后，将工具结果汇入上下文
  - 调用 answer_model（最终回复模型）综合工具结果，生成用户可见的最终答案
  - 注入步骤边界指令（计划模式下防止越界和无限工具调用）
  - 有图片时走视觉路径（VISION_BASE_URL）

与 CallModelNode 的区别：
  - 使用 answer_model（而非 tool_model）
  - 不绑定工具（boundary 注入已限制工具调用）
  - 支持内容审核错误的优雅降级

工厂注入：
  - tools: 仅用于与 CallModelNode 保持结构对称，此节点实际不使用
"""
import asyncio
import logging

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, SystemMessage

from config import VISION_MODEL
from graph.nodes.base import BaseNode
from graph.state import GraphState
from llm.chat import get_chat_llm, get_vision_llm

logger = logging.getLogger("graph.nodes.call_model_after_tool")

# 内容审核错误标识（MiniMax 等厂商的特定错误码）
_AUDIT_MARKERS = ("1027", "sensitive")

# 每步最多保留最近消息条数（多步执行场景需要早期工具结果）
_MAX_MESSAGES = 20


class CallModelAfterToolNode(BaseNode):
    """
    工具执行后的 LLM 综合节点。

    通过 __init__ 注入工具列表（仅为接口对称，不使用工具）。
    """

    def __init__(self, tools: list) -> None:
        """
        参数：
            tools: 工具列表（保留接口，此节点不绑定工具）
        """
        self._tools = tools  # 保留以备扩展，本节点不绑定工具

    @property
    def name(self) -> str:
        return "call_model_after_tool"

    async def execute(self, state: GraphState) -> dict:
        """
        综合工具结果，生成最终回复。

        步骤：
          1. 保留最近 _MAX_MESSAGES 条消息（避免上下文过长）
          2. 注入步骤边界指令（计划模式）
          3. 含图片 → 视觉路径
          4. 无图片 → 主 LLM 路径
        """
        model       = state["answer_model"]
        temperature = state["temperature"]
        conv_id     = state.get("conv_id", "")
        plan        = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)

        messages = list(state["messages"])
        messages = messages[-_MAX_MESSAGES:]  # 截取最近消息

        # ── 计划模式：注入步骤边界指令 ──────────────────────────────────────
        if plan and current_idx < len(plan):
            messages = self._inject_boundary(messages, plan, current_idx)

        logger.info(
            "call_model_after_tool 开始 | conv=%s | model=%s | step=%s/%s | messages=%d",
            conv_id, model,
            current_idx + 1 if plan else "-",
            len(plan) if plan else "-",
            len(messages),
        )

        if state.get("images"):
            return await self._vision_path(state, messages, model, temperature, conv_id)
        else:
            return await self._llm_path(messages, model, temperature, conv_id)

    def _inject_boundary(
        self,
        messages: list,
        plan: list,
        current_idx: int,
    ) -> list:
        """
        向 SystemMessage 末尾追加步骤边界指令。

        目的：
          - 防止模型越界回答后续步骤
          - 防止已有足够工具结果时继续调用工具（无限循环）
        """
        step       = plan[current_idx]
        total      = len(plan)
        tool_count = sum(1 for m in messages if type(m).__name__ == "ToolMessage")
        boundary   = (
            f"\n\n===当前执行步骤===\n"
            f"步骤 {current_idx + 1}/{total}：{step['title']}\n"
            f"任务：{step['description']}\n"
            f"本步已调用工具 {tool_count} 次。"
        )
        if tool_count >= 2:
            boundary += (
                "已有足够工具结果，请直接给出本步骤的结论，"
                "不要继续调用工具，不要回答后续步骤的内容。"
            )

        # 追加到 SystemMessage 末尾（保留原始上下文）
        if messages and type(messages[0]).__name__ == "SystemMessage":
            messages = [SystemMessage(content=messages[0].content + boundary)] + messages[1:]
        return messages

    async def _vision_path(
        self,
        state: GraphState,
        messages: list,
        model: str,
        temperature: float,
        conv_id: str,
    ) -> dict:
        """含图片时走视觉路径，此节点只生成最终回复，不绑定工具。"""
        vision_model = VISION_MODEL or model
        vision_llm   = get_vision_llm(model=vision_model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        logger.info(
            "call_model_after_tool (vision) 请求发出 | conv=%s | model=%s | msgs=%d",
            conv_id, vision_model, len(oai_messages),
        )

        try:
            completion = await vision_llm.ainvoke(oai_messages, timeout=180.0)
        except asyncio.TimeoutError:
            logger.error("call_model_after_tool (vision) 超时 | conv=%s", conv_id)
            raise
        except Exception as exc:
            # 内容审核降级
            if self._is_audit_error(exc):
                logger.warning("call_model_after_tool (vision) 触发内容审核 | conv=%s", conv_id)
                return self._audit_fallback()
            logger.error(
                "call_model_after_tool (vision) 异常 | conv=%s | error=%s",
                conv_id, exc, exc_info=True,
            )
            raise

        full_content = completion.choices[0].message.content or ""
        logger.info(
            "call_model_after_tool (vision) 完成 | conv=%s | content_len=%d | preview='%.100s'",
            conv_id, len(full_content), full_content,
        )
        return {"messages": [AIMessage(content=full_content)], "full_response": full_content}

    async def _llm_path(
        self,
        messages: list,
        model: str,
        temperature: float,
        conv_id: str,
    ) -> dict:
        """无图片时走主 LLM 路径，不绑定工具，全程流式输出。"""
        llm          = get_chat_llm(model=model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        logger.info(
            "call_model_after_tool LLM 请求（流式） | conv=%s | model=%s | msgs=%d",
            conv_id, model, len(oai_messages),
        )

        content_parts: list[str] = []
        token_count = 0

        try:
            async for delta in llm.astream(oai_messages, temperature=temperature):
                content_parts.append(delta)
                token_count += 1
                await adispatch_custom_event("llm_token", {"content": delta, "node": "call_model_after_tool"})
        except asyncio.TimeoutError:
            logger.error("call_model_after_tool 超时 | conv=%s | model=%s", conv_id, model)
            raise
        except Exception as exc:
            if self._is_audit_error(exc):
                logger.warning("call_model_after_tool 触发内容审核 | conv=%s", conv_id)
                return self._audit_fallback()
            logger.error(
                "call_model_after_tool LLM 异常 | conv=%s | model=%s | error=%s",
                conv_id, model, exc, exc_info=True,
            )
            raise

        full_content = "".join(content_parts)
        logger.info(
            "call_model_after_tool 流式完成 | conv=%s | model=%s | tokens=%d | content_len=%d",
            conv_id, model, token_count, len(full_content),
        )
        return {"messages": [AIMessage(content=full_content)], "full_response": full_content}

    @staticmethod
    def _is_audit_error(exc: Exception) -> bool:
        """判断是否为内容审核错误（MiniMax 1027 等）。"""
        err_str = str(exc)
        return any(marker in err_str for marker in _AUDIT_MARKERS)

    @staticmethod
    def _audit_fallback() -> dict:
        """内容审核错误时的优雅降级响应。"""
        msg = "抱歉，该内容触发了模型安全审核，无法生成回复。请换个方式描述问题。"
        return {"messages": [], "full_response": msg}
