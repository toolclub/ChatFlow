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

共享逻辑（继承自 BaseNode）：
  - _stream_tokens  → 流式 LLM 调用 + thinking token 处理
  - _is_audit_error → 内容审核错误判断
  - _audit_fallback → 审核降级响应

工厂注入：
  - tools: 仅用于与 CallModelNode 保持结构对称，此节点实际不使用
"""
import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config import VISION_MODEL
from graph.nodes.base import BaseNode
from graph.state import GraphState
from llm.chat import get_chat_llm, get_vision_llm

logger = logging.getLogger("graph.nodes.call_model_after_tool")

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
        综合工具结果，生成最终回复（或继续调用更多工具）。

        工具绑定策略：
          - 无计划模式 + 有沙箱工具 → 绑定工具（允许 write 后继续 execute）
          - 计划模式中间步骤 → 绑定工具（允许多轮工具调用）
          - 计划模式末步 → 不绑工具（强制流式生成最终产品）
        """
        model       = state["answer_model"]
        temperature = state["temperature"]
        conv_id     = state.get("conv_id", "")
        plan        = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)
        route       = state.get("route", "")

        is_last = not plan or current_idx >= len(plan) - 1

        messages = list(state["messages"])
        messages = messages[-_MAX_MESSAGES:]

        # ── 计划模式：注入步骤边界指令 ──────────────────────────────────────
        if plan and current_idx < len(plan):
            messages = self._inject_boundary(messages, plan, current_idx)

        # ── 工具绑定：始终绑定全部工具，让模型自主决定是否搜索/执行代码 ────
        use_tools = bool(self._tools)
        tools_schema = self._tools_to_openai_schema(self._tools) if use_tools else None

        logger.info(
            "call_model_after_tool 开始 | conv=%s | model=%s | step=%s/%s | "
            "is_last=%s | use_tools=%s | messages=%d",
            conv_id, model,
            current_idx + 1 if plan else "-",
            len(plan) if plan else "-",
            is_last, use_tools,
            len(messages),
        )

        vision_desc = state.get("vision_description", "")
        if state.get("images") and not vision_desc:
            return await self._vision_path(state, messages, model, temperature, conv_id, is_last=is_last)
        else:
            return await self._llm_path(
                messages, model, temperature, conv_id,
                is_last=is_last, tools_schema=tools_schema,
            )

    def _inject_boundary(
        self,
        messages: list,
        plan: list,
        current_idx: int,
    ) -> list:
        """
        步骤边界处理。

        - 非末步：完全重建消息集，隔离原始用户意图，只保留步骤指令 + 工具结果，
                  防止模型看到最终目标后提前生成完整产品。
        - 末步：在 SystemMessage 末尾追加历史步骤结果 + 生成指令。
        """
        step       = plan[current_idx]
        total      = len(plan)
        is_last    = current_idx >= total - 1
        tool_count = sum(1 for m in messages if type(m).__name__ == "ToolMessage")

        if not is_last:
            return self._build_focused_step_messages(messages, step, tool_count)

        # ── 末步：注入历史步骤结果 + 生成最终回复指令 ──────────────────────
        prev_results: list[str] = []
        for i in range(current_idx):
            result = plan[i].get("result", "")
            if result:
                prev_results.append(
                    f"【步骤{i + 1}：{plan[i]['title']}的执行结果】\n{result[:1500]}"
                )

        boundary = ""
        if prev_results:
            boundary += "\n\n" + "\n\n".join(prev_results)

        # 检查上一次工具执行是否失败
        last_tool_failed = False
        for m in reversed(messages):
            if type(m).__name__ == "ToolMessage":
                content_str = str(getattr(m, "content", ""))
                if any(kw in content_str for kw in ("exit_code=1", "执行失败", "Traceback", "Error", "error")):
                    last_tool_failed = True
                break

        boundary += (
            f"\n\n===当前执行步骤 {current_idx + 1}/{total}（最后一步）===\n"
            f"标题：{step['title']}\n"
            f"任务：{step['description']}\n"
            f"本步已调用工具 {tool_count} 次。\n"
        )
        if last_tool_failed:
            boundary += (
                "上一次代码执行失败了。请修复代码并使用 execute_code/run_shell 重新在沙箱中执行，"
                "验证通过后再给出结论。不要直接把代码写在回复里。"
            )
        else:
            boundary += "请基于以上所有信息和工具结果给出完整的最终回复。如需补充搜索或执行代码可继续使用工具。"

        if messages and type(messages[0]).__name__ == "SystemMessage":
            messages = [SystemMessage(content=messages[0].content + boundary)] + messages[1:]
        return messages

    def _build_focused_step_messages(
        self,
        messages: list,
        step: dict,
        tool_count: int,
    ) -> list:
        """
        中间步骤：重建聚焦消息集。

        丢弃原始对话历史（含用户最终意图），只保留：
          1. 聚焦系统提示（只允许输出摘要）
          2. 步骤指令 HumanMessage
          3. 工具调用 AIMessage（含 tool_calls）
          4. 工具返回 ToolMessage(s)

        这样模型完全看不到 "帮我做网页" 之类的最终目标，
        只能根据工具结果写出当前步骤的信息摘要。
        """
        # 提取工具调用 AIMessage（最后一个含 tool_calls 的）
        ai_tool_msg = None
        for m in messages:
            if type(m).__name__ == "AIMessage" and getattr(m, "tool_calls", None):
                ai_tool_msg = m

        # 只提取与 ai_tool_msg 的 tool_call_id 对应的 ToolMessage。
        # 多步执行时 state["messages"] 会积累前几步的 ToolMessage，
        # 若一并发给 MiniMax 会触发 "tool call id is invalid (2013)"。
        if ai_tool_msg and getattr(ai_tool_msg, "tool_calls", None):
            valid_ids = {tc["id"] for tc in ai_tool_msg.tool_calls}
            tool_msgs = [
                m for m in messages
                if type(m).__name__ == "ToolMessage"
                and getattr(m, "tool_call_id", None) in valid_ids
            ]
        else:
            tool_msgs = [m for m in messages if type(m).__name__ == "ToolMessage"]

        from prompts import load_prompt as _lp
        focused_system = SystemMessage(content=_lp(
            "nodes/after_tool_step", step_title=step['title'],
        ))
        step_instruction = HumanMessage(content=(
            f"当前步骤任务：{step['description']}\n"
            f"本步已调用工具 {tool_count} 次，请分析工具结果并给出本步骤的信息摘要。"
        ))

        new_messages: list = [focused_system, step_instruction]
        if ai_tool_msg:
            new_messages.append(ai_tool_msg)
        new_messages.extend(tool_msgs)
        return new_messages

    # 视觉调用超时（秒）：GLM-4.6V 推理模式较慢
    _VISION_TIMEOUT = 300.0

    async def _vision_path(
        self,
        state: GraphState,
        messages: list,
        model: str,
        temperature: float,
        conv_id: str,
        is_last: bool = True,
    ) -> dict:
        """含图片时走视觉路径。所有步骤均流式输出（中间步骤也实时可见）。"""
        vision_model = VISION_MODEL or model
        vision_llm   = get_vision_llm(model=vision_model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        step_label = "末步" if is_last else "中间步骤"
        logger.info(
            "call_model_after_tool (vision) 请求发出（流式，%s） | conv=%s | model=%s | msgs=%d",
            step_label, conv_id, vision_model, len(oai_messages),
        )
        from logging_config import log_prompt
        log_prompt(conv_id, "call_model_after_tool_vision", vision_model, oai_messages)

        try:
            result = await self._stream_tokens(
                vision_llm, oai_messages, temperature, conv_id,
                node="call_model_after_tool",
                timeout=self._VISION_TIMEOUT,
            )
            stream_err = result.pop("_stream_error", None)
            if stream_err:
                if self._is_audit_error(stream_err):
                    logger.warning("call_model_after_tool (vision) 流式触发内容审核 | conv=%s", conv_id)
                    return self._audit_fallback()
                logger.warning(
                    "call_model_after_tool (vision) 流式异常但保留部分内容 | conv=%s | partial_len=%d",
                    conv_id, len(result.get("full_response", "")),
                )
            return result
        except asyncio.TimeoutError:
            logger.error(
                "call_model_after_tool (vision) 超时 %.0fs | conv=%s | model=%s",
                self._VISION_TIMEOUT, conv_id, vision_model,
            )
            raise
        except Exception as exc:
            if self._is_audit_error(exc):
                logger.warning("call_model_after_tool (vision) 触发内容审核 | conv=%s", conv_id)
                return self._audit_fallback()
            logger.error(
                "call_model_after_tool (vision) 异常 | conv=%s | error=%s",
                conv_id, exc, exc_info=True,
            )
            raise

    async def _llm_path(
        self,
        messages: list,
        model: str,
        temperature: float,
        conv_id: str,
        is_last: bool = True,
        tools_schema: list | None = None,
    ) -> dict:
        """
        主 LLM 路径。

        tools_schema 非空时：非流式调用（需要完整 tool_calls JSON），
                           模型可继续调用工具（如 write 后 execute）。
        tools_schema 为空时：流式调用（逐 token 输出最终回复）。
        """
        llm          = get_chat_llm(model=model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        from logging_config import log_prompt

        step_label = "末步" if is_last else "中间步骤"
        mode_label = "非流式+工具" if tools_schema else "流式"
        logger.info(
            "call_model_after_tool LLM 请求（%s，%s） | conv=%s | model=%s | msgs=%d",
            mode_label, step_label, conv_id, model, len(oai_messages),
        )
        log_prompt(conv_id, "call_model_after_tool", model, oai_messages)
        try:
            # ── 绑定工具时：流式调用（thinking/content 实时推送 + tool_calls 收集）──
            if tools_schema:
                result = await self._stream_tokens_with_tools(
                    llm, oai_messages, tools_schema, temperature,
                    conv_id, "call_model_after_tool",
                )
                stream_err = result.pop("_stream_error", None)
                if stream_err:
                    if self._is_audit_error(stream_err):
                        return self._audit_fallback()
                return result

            # ── 不绑工具时：流式输出 ──
            result = await self._stream_tokens(
                llm, oai_messages, temperature, conv_id, node="call_model_after_tool"
            )
            # _stream_tokens 中途异常时返回 partial content + _stream_error 标记
            stream_err = result.pop("_stream_error", None)
            if stream_err:
                if self._is_audit_error(stream_err):
                    logger.warning("call_model_after_tool 流式触发内容审核 | conv=%s", conv_id)
                    return self._audit_fallback()
                logger.warning(
                    "call_model_after_tool 流式异常但保留部分内容 | conv=%s | partial_len=%d",
                    conv_id, len(result.get("full_response", "")),
                )
            return result
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

    # _stream_tokens / _is_audit_error / _audit_fallback 继承自 BaseNode
