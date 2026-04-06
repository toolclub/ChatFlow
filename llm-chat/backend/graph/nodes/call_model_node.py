"""
CallModelNode：主 LLM 推理节点

职责：
  - 从 state 读取 tool_model 和 temperature，动态获取 LLM（已按 key 缓存）
  - 若有执行计划，在本地消息副本中注入当前步骤上下文
  - 将 state.messages 送入 LLM
  - 有图片时走视觉路径（VISION_BASE_URL）
  - 无图片时走主 LLM 路径（LLM_BASE_URL），使用原生 AsyncOpenAI

工厂注入：
  - tools: 工具列表，search/search_code 路由时绑定工具 schema

路由逻辑（由后续的 should_continue 边决定）：
  - 返回 tool_calls → ToolNode 执行工具 → call_model_after_tool
  - 返回最终回复  → reflector（有计划） or save_response（无计划）
"""
import asyncio
import logging

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage

from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
from graph.nodes.base import BaseNode
from graph.state import GraphState
from llm.chat import get_chat_llm, get_vision_llm

logger = logging.getLogger("graph.nodes.call_model")


class CallModelNode(BaseNode):
    """
    主 LLM 推理节点。

    通过 __init__ 注入工具列表，避免全局依赖。
    """

    def __init__(self, tools: list) -> None:
        """
        参数：
            tools: LangChain BaseTool 列表，search/search_code 路由时绑定工具
        """
        self._tools = tools

    @property
    def name(self) -> str:
        return "call_model"

    async def execute(self, state: GraphState) -> dict:
        """
        核心推理逻辑：

          1. 确定是否启用工具（search/search_code 路由）
          2. 注入步骤上下文（有计划且首次调用时）
          3. 含图片 → 视觉路径（VISION_BASE_URL + VISION_MODEL）
          4. 无图片 → 主 LLM 路径（LLM_BASE_URL + tool_model）
          5. 将响应转回 LangChain AIMessage 格式（供 ToolNode/should_continue 使用）
        """
        route       = state.get("route", "")
        model       = state.get("tool_model") or state["model"]
        temperature = state["temperature"]
        conv_id     = state.get("conv_id", "")

        # search/search_code 路由绑定工具；chat/code 路由不绑定
        use_tools    = bool(self._tools and (not route or route in ("search", "search_code")))
        tools_schema = self._tools_to_openai_schema(self._tools) if use_tools else None

        # ── 消息列表（本地副本，避免修改 state） ────────────────────────────
        messages = list(state["messages"])

        # ── 首次执行时注入步骤上下文 ────────────────────────────────────────
        plan       = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)
        step_iters  = state.get("step_iterations", 0)

        logger.info(
            "call_model 开始 | conv=%s | model=%s | use_tools=%s | "
            "step=%s/%s | iter=%s | messages=%d",
            conv_id, model, use_tools,
            current_idx + 1 if plan else "-",
            len(plan) if plan else "-",
            step_iters,
            len(messages),
        )

        # 仅首次调用（step_iters==0 且 current_idx==0）时注入步骤上下文
        if plan and current_idx < len(plan) and current_idx == 0 and step_iters == 0:
            step     = plan[current_idx]
            total    = len(plan)
            step_ctx = (
                f"\n\n---\n**[执行步骤 {current_idx + 1}/{total}]: {step['title']}**\n"
                f"具体任务：{step['description']}\n"
                "请使用工具完成此步骤，收集所需信息。"
            )
            # 追加到最后的 HumanMessage（仅用于本次 LLM 调用，不写回 state）
            if messages and messages[-1].__class__.__name__ == "HumanMessage":
                last_content = messages[-1].content
                if isinstance(last_content, list):
                    # 多模态消息：追加文本部分
                    messages[-1] = HumanMessage(
                        content=list(last_content) + [{"type": "text", "text": step_ctx}]
                    )
                else:
                    messages[-1] = HumanMessage(content=str(last_content) + step_ctx)

        # ── 路径选择 ────────────────────────────────────────────────────────
        # VisionNode 已在上游完成图片分析，vision_description 写入 state。
        # retrieve_context 将描述注入为文字消息，主模型无需视觉能力。
        # 仅在 VisionNode 降级（描述为空）且原始图片仍在 state 时，
        # 才回退 vision_path 让视觉模型直接处理图片。
        vision_desc = state.get("vision_description", "")
        if state.get("images") and not vision_desc:
            return await self._vision_path(state, messages, use_tools, tools_schema, conv_id)
        else:
            return await self._llm_path(state, messages, model, temperature, use_tools, tools_schema, conv_id)

    # 视觉调用超时（秒）：GLM-4.6V 推理模式较慢，给足 5 分钟
    _VISION_TIMEOUT = 300.0

    async def _vision_path(
        self,
        state: GraphState,
        messages: list,
        use_tools: bool,
        tools_schema: list | None,
        conv_id: str,
    ) -> dict:
        """
        含图片时走视觉路径：使用 VISION_BASE_URL + VISION_MODEL。

        - 无工具时：流式输出（同主 LLM 路径），逐 token 推送给前端
        - 有工具时：非流式，确保 function_calling JSON 完整
        - 超时：300s（GLM-4.6V 推理模式耗时较长）
        """
        temperature  = state["temperature"]
        vision_model = VISION_MODEL or state.get("tool_model") or state["model"]
        vision_llm   = get_vision_llm(model=vision_model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        logger.info(
            "call_model (vision) 请求发出 | conv=%s | model=%s | use_tools=%s | msgs=%d",
            conv_id, vision_model, use_tools, len(oai_messages),
        )

        try:
            if use_tools:
                # 工具调用须非流式，拿到完整 JSON
                completion = await vision_llm.ainvoke(
                    oai_messages,
                    tools=tools_schema,
                    timeout=self._VISION_TIMEOUT,
                )
                return self._build_response(completion, conv_id, "vision")
            else:
                # 无工具时流式，逐 token 推送前端（与主 LLM 路径一致）
                return await self._stream_tokens(
                    vision_llm, oai_messages, temperature, conv_id,
                    node="call_model_vision",
                    timeout=self._VISION_TIMEOUT,
                )
        except asyncio.TimeoutError:
            logger.error(
                "call_model (vision) 超时 %.0fs | conv=%s | model=%s",
                self._VISION_TIMEOUT, conv_id, vision_model,
            )
            raise
        except Exception as exc:
            logger.error(
                "call_model (vision) 异常 | conv=%s | model=%s | error=%s",
                conv_id, vision_model, exc, exc_info=True,
            )
            raise

    async def _llm_path(
        self,
        state: GraphState,
        messages: list,
        model: str,
        temperature: float,
        use_tools: bool,
        tools_schema: list | None,
        conv_id: str,
    ) -> dict:
        """
        无图片时走主 LLM 路径：使用 LLM_BASE_URL + tool_model。

          - use_tools=True  → 非流式（工具调用需要完整 JSON）
          - use_tools=False → 流式（逐 token 派发 llm_token 事件供前端实时渲染）
        """
        llm          = get_chat_llm(model=model, temperature=temperature)
        oai_messages = self._to_openai_messages(messages)

        logger.info(
            "call_model LLM 请求发出 | conv=%s | model=%s | use_tools=%s | msgs=%d",
            conv_id, model, use_tools, len(oai_messages),
        )

        try:
            if use_tools:
                # 绑定工具时非流式，确保 tool_calls JSON 完整返回
                completion = await llm.ainvoke(oai_messages, tools=tools_schema, timeout=180.0)
                return self._build_response(completion, conv_id, "llm")
            else:
                # 无工具时流式，逐 token 通过 adispatch_custom_event 推送给前端
                return await self._stream_tokens(llm, oai_messages, temperature, conv_id, "call_model")
        except asyncio.TimeoutError:
            logger.error("call_model 超时 | conv=%s | model=%s", conv_id, model)
            raise
        except Exception as exc:
            logger.error("call_model LLM 异常 | conv=%s | model=%s | error=%s", conv_id, model, exc, exc_info=True)
            raise

    @staticmethod
    async def _stream_tokens(
        llm,
        oai_messages: list,
        temperature: float,
        conv_id: str,
        node: str,
        timeout: float = 180.0,
    ) -> dict:
        """
        流式 LLM 调用：逐 token yield，通过 adispatch_custom_event 发出事件。

        LLMStreamHandler 在 astream_events 中监听这些事件，实时推送给前端。
        节点返回时 full_response 包含完整内容，CallModelEndHandler 因
        ctx.*_streamed=True 而跳过重复发送。

        支持推理模型（GLM/DeepSeek-R1 等）的 thinking token：
          - "\x00THINK\x00" 前缀的 delta 作为 thinking 事件分发
          - 普通 delta 作为 llm_token 事件分发
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        token_count = 0
        _THINK_PREFIX = "\x00THINK\x00"

        async for delta in llm.astream(oai_messages, temperature=temperature, timeout=timeout):
            token_count += 1
            if delta.startswith(_THINK_PREFIX):
                # 推理 token
                thinking_text = delta[len(_THINK_PREFIX):]
                thinking_parts.append(thinking_text)
                await adispatch_custom_event("llm_thinking", {"content": thinking_text, "node": node})
            else:
                content_parts.append(delta)
                await adispatch_custom_event("llm_token", {"content": delta, "node": node})

        full_content = "".join(content_parts)
        full_thinking = "".join(thinking_parts)
        logger.info(
            "call_model 流式完成 | conv=%s | node=%s | tokens=%d | content_len=%d | thinking_len=%d",
            conv_id, node, token_count, len(full_content), len(full_thinking),
        )
        result: dict = {"messages": [AIMessage(content=full_content)], "full_response": full_content}
        if full_thinking:
            result["full_thinking"] = full_thinking
        return result

    def _build_response(self, completion, conv_id: str, path: str) -> dict:
        """
        从 ChatCompletion 构建节点返回值。

        将 OpenAI tool_calls 转为 LangChain AIMessage 格式，
        确保 should_continue 边和 ToolNode 能正确处理。
        """
        msg            = completion.choices[0].message
        content        = msg.content or ""
        oai_tool_calls = msg.tool_calls or []

        if oai_tool_calls:
            lc_tool_calls = self._convert_oai_tool_calls(oai_tool_calls)
            for tc in lc_tool_calls:
                logger.info(
                    "call_model (%s) tool_call | conv=%s | name=%s | args=%.200s",
                    path, conv_id, tc["name"], str(tc["args"]),
                )
            ai_msg = AIMessage(content=content, tool_calls=lc_tool_calls)
            logger.info(
                "call_model (%s) 完成(tool_calls) | conv=%s | tool_calls=%d | content_len=%d",
                path, conv_id, len(lc_tool_calls), len(content),
            )
        else:
            ai_msg = AIMessage(content=content)
            logger.info(
                "call_model (%s) 完成 | conv=%s | content_len=%d | preview='%.100s'",
                path, conv_id, len(content), content,
            )

        return {"messages": [ai_msg], "full_response": content}
