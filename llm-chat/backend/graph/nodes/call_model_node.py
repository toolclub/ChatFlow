"""
CallModelNode：主 LLM 推理节点

职责：
  - 预检：确定性检测「网页/UI 生成」请求，若缺少风格信息直接生成澄清卡片，跳过 LLM 调用
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
import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
from graph.nodes.base import BaseNode
from graph.state import GraphState
from llm.chat import get_chat_llm, get_vision_llm

logger = logging.getLogger("graph.nodes.call_model")

# ── 网页生成澄清预检 ──────────────────────────────────────────────────────────
# 匹配用户希望生成网页/UI 的关键词
_WEBPAGE_KEYWORDS = re.compile(
    r"(网页|页面|html|h5|落地页|官网|网站|ui\s*界面|前端界面|web\s*页)", re.IGNORECASE
)
# 已包含风格信息的关键词（说明用户已主动指定，不再追问）
_STYLE_KEYWORDS = re.compile(
    r"(风格|色调|主题色|配色|设计风格|简约|商务|科技感|严肃|活泼|补充说明|设计风格是|色调是)", re.IGNORECASE
)


def _needs_webpage_clarification(user_msg: str) -> bool:
    """
    判断是否需要网页澄清预检：
      - 用户请求中含有生成网页/UI 的意图关键词
      - 且尚未包含设计风格/色调等偏好信息（澄清后的第二轮已包含）
    """
    return bool(_WEBPAGE_KEYWORDS.search(user_msg)) and not bool(_STYLE_KEYWORDS.search(user_msg))


from prompts import load_json_prompt, load_prompt as _load_prompt

_WEBPAGE_CLARIFICATION = load_json_prompt("clarification/webpage")


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

          0. 预检：网页/UI 生成且缺少风格信息 → 直接返回澄清卡片，跳过 LLM
          1. 确定是否启用工具（search/search_code 路由）
          2. 注入步骤上下文（有计划且首次调用时）
          3. 含图片 → 视觉路径（VISION_BASE_URL + VISION_MODEL）
          4. 无图片 → 主 LLM 路径（LLM_BASE_URL + tool_model）
          5. 将响应转回 LangChain AIMessage 格式（供 ToolNode/should_continue 使用）
        """
        # ── 0. 网页澄清预检（确定性，不依赖 LLM 判断） ──────────────────────
        # 仅在首轮调用（无计划步骤、未处于多步执行中）时触发，
        # 避免澄清后的第二轮再次拦截。
        plan        = state.get("plan", [])
        step_iters  = state.get("step_iterations", 0)
        cur_idx     = state.get("current_step_index", 0)
        user_msg    = state.get("user_message", "")

        # plan 为空说明：
        #   - chat/code 路由（planner 直接跳过规划）
        #   - search/search_code 路由且 planner_node 的澄清预检已返回空计划
        # 有图片时 planner_node 不拦截（图片即风格参考），plan 非空，此处不会触发。
        if not plan and step_iters == 0 and cur_idx == 0 and _needs_webpage_clarification(user_msg):
            conv_id = state.get("conv_id", "")
            logger.info(
                "call_model 网页澄清预检命中，跳过 LLM | conv=%s | user_msg=%.60s",
                conv_id, user_msg,
            )
            clar_json = json.dumps(_WEBPAGE_CLARIFICATION, ensure_ascii=False)
            fake_response = f"[NEED_CLARIFICATION]{clar_json}[/NEED_CLARIFICATION]"
            return {
                "messages":     [AIMessage(content=fake_response)],
                "full_response": fake_response,
            }

        route       = state.get("route", "")
        model       = state.get("tool_model") or state["model"]
        temperature = state["temperature"]
        conv_id     = state.get("conv_id", "")

        # 工具绑定策略：有工具就全部绑定，让模型自主判断何时搜索/执行代码
        is_intermediate_plan_step = bool(plan) and cur_idx < len(plan) - 1
        use_tools = bool(self._tools)
        tools_schema = self._tools_to_openai_schema(self._tools) if use_tools else None

        # ── 消息列表（本地副本，步骤 1+ 使用聚焦上下文，不喂完整历史） ────────
        current_idx = cur_idx

        if plan and current_idx > 0:
            # 步骤 1+：用 GraphState.plan 中已有的步骤结果构建聚焦消息，
            # 完全不依赖 state["messages"] 的积累历史，实现真正的上下文隔离。
            messages = self._build_focused_step_messages(state, plan, current_idx)
        else:
            messages = list(state["messages"])

        logger.info(
            "call_model 开始 | conv=%s | model=%s | use_tools=%s | "
            "step=%s/%s | iter=%s | messages=%d",
            conv_id, model, use_tools,
            current_idx + 1 if plan else "-",
            len(plan) if plan else "-",
            step_iters,
            len(messages),
        )

        # 步骤 0：首次调用时注入步骤上下文到最后一条 HumanMessage
        if plan and current_idx == 0 and current_idx < len(plan) and step_iters == 0:
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
        # 步骤 1+ 聚焦消息已含文字视觉描述，不需要再走视觉路径
        if state.get("images") and not vision_desc and current_idx == 0:
            return await self._vision_path(state, messages, use_tools, tools_schema, conv_id)
        else:
            return await self._llm_path(state, messages, model, temperature, use_tools, tools_schema, conv_id)

    # ══════════════════════════════════════════════════════════════════════════
    # 聚焦步骤消息构建（步骤 1+ 专用）
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _build_focused_step_messages(
        state: GraphState,
        plan: list,
        current_idx: int,
    ) -> list:
        """
        为步骤 1+ 构建聚焦上下文消息，完全替代 state["messages"] 的完整历史。

        设计原则：
          - 模型只看到：总目标 + 已完成步骤摘要 + 当前步骤指令
          - 不含原始对话历史、mid_term_summary、long_term_memories
          - 末步使用对话的系统提示（保持风格/人格一致）
          - 中间步骤使用聚焦执行者系统提示（防止提前生成最终产品）

        上下文层次：
          SystemMessage → HumanMessage（总目标）→ AIMessage（步骤N结果）× N
          → HumanMessage（当前步骤指令）
        """
        from config import DEFAULT_SYSTEM_PROMPT
        from memory import store as memory_store

        total       = len(plan)
        is_last     = current_idx >= total - 1
        # 续写场景：user_message 是"继续"，用 plan_goal 还原原始任务目标
        user_msg    = state.get("plan_goal") or state.get("user_message", "")
        vision_desc = state.get("vision_description", "")
        conv_id     = state.get("conv_id", "")

        # ── 系统提示 ────────────────────────────────────────────────────────
        if is_last:
            # 末步：用对话的自定义系统提示，确保最终回复风格符合用户期望
            conv = memory_store.get(conv_id)
            system_content = (
                conv.system_prompt
                if conv and conv.system_prompt
                else DEFAULT_SYSTEM_PROMPT
            )
        else:
            # 中间步骤：专注执行者角色，防止提前生成最终产品
            system_content = _load_prompt("nodes/call_model_step")

        # ── 任务总目标 ───────────────────────────────────────────────────────
        goal = user_msg
        if vision_desc:
            goal = f"{user_msg}\n\n[图片内容分析]\n{vision_desc}" if user_msg.strip() else vision_desc

        messages: list = [
            SystemMessage(content=system_content),
            HumanMessage(content=f"任务总目标：{goal}"),
        ]

        # ── 已完成步骤的执行结果（来自 GraphState.plan[i].result） ────────────
        # reflector_node 在每步完成时写入 plan[i].result，这里直接读取。
        # 不需要 DB 读取，GraphState 是运行时权威数据；DB 只做持久化副本。
        for i in range(current_idx):
            result = plan[i].get("result", "")
            title  = plan[i].get("title", f"步骤{i + 1}")
            if result:
                messages.append(
                    AIMessage(content=f"【步骤{i + 1}：{title}的执行结果】\n{result}")
                )

        # ── 当前步骤指令（含断点续传的部分结果） ──────────────────────────────
        current_step = plan[current_idx]
        # 当前步骤可能有部分结果（崩溃时 _save_partial_plan_step 写入的）
        # 若有，注入到上下文中让模型从中断处继续，而非从头开始
        partial_result = current_step.get("result", "")

        if is_last:
            step_instruction = (
                f"请基于以上所有步骤的执行结果，完成最终任务：\n"
                f"{current_step['description']}"
            )
            if partial_result:
                step_instruction += (
                    f"\n\n⚠️ 上次执行此步骤时中断了，以下是已生成的部分内容：\n"
                    f"{partial_result}\n\n"
                    f"请从中断处接着输出，不要重复上面已有的内容。"
                )
        else:
            step_instruction = (
                f"\n\n---\n**[执行步骤 {current_idx + 1}/{total}]: {current_step['title']}**\n"
                f"具体任务：{current_step['description']}\n"
                "请使用工具完成此步骤，收集所需信息。"
            )
            if partial_result:
                step_instruction += (
                    f"\n\n上次执行中断，以下是已有的部分结果：\n{partial_result}\n"
                    f"请从中断处继续。"
                )
        messages.append(HumanMessage(content=step_instruction))

        logger.info(
            "_build_focused_step_messages | step=%d/%d | is_last=%s | "
            "prev_results=%d | msg_count=%d",
            current_idx + 1, total, is_last,
            sum(1 for i in range(current_idx) if plan[i].get("result")),
            len(messages),
        )
        return messages

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
        from logging_config import log_prompt
        log_prompt(conv_id, "call_model_vision", vision_model, oai_messages)

        try:
            if use_tools:
                # 工具调用也流式：thinking 实时可见 + tool_calls 收集
                result = await self._stream_tokens_with_tools(
                    vision_llm, oai_messages, tools_schema, temperature,
                    conv_id, "call_model_vision", timeout=self._VISION_TIMEOUT,
                )
                stream_err = result.pop("_stream_error", None)
                if stream_err and self._is_audit_error(stream_err):
                    return self._audit_fallback()
                return result
            else:
                # 无工具时流式，使用 BaseNode._stream_tokens 逐 token 推送前端
                result = await self._stream_tokens(
                    vision_llm, oai_messages, temperature, conv_id,
                    node="call_model_vision",
                    timeout=self._VISION_TIMEOUT,
                )
                stream_err = result.pop("_stream_error", None)
                if stream_err:
                    if self._is_audit_error(stream_err):
                        logger.warning("call_model (vision) 流式触发内容审核 | conv=%s", conv_id)
                        return self._audit_fallback()
                return result
        except asyncio.TimeoutError:
            logger.error(
                "call_model (vision) 超时 %.0fs | conv=%s | model=%s",
                self._VISION_TIMEOUT, conv_id, vision_model,
            )
            raise
        except Exception as exc:
            if self._is_audit_error(exc):
                logger.warning("call_model (vision) 触发内容审核 | conv=%s", conv_id)
                return self._audit_fallback()
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
        from logging_config import log_prompt
        log_prompt(conv_id, "call_model", model, oai_messages)

        try:
            if use_tools:
                # 绑定工具时也走流式：thinking/content 实时推送，tool_calls 同步收集
                result = await self._stream_tokens_with_tools(
                    llm, oai_messages, tools_schema, temperature, conv_id, "call_model",
                )
                stream_err = result.pop("_stream_error", None)
                if stream_err:
                    if self._is_audit_error(stream_err):
                        return self._audit_fallback()
                return result
            else:
                # 无工具时流式，逐 token 通过 adispatch_custom_event 推送给前端
                result = await self._stream_tokens(llm, oai_messages, temperature, conv_id, "call_model")
                # _stream_tokens 中途异常时返回 partial content + _stream_error 标记
                # 审核错误：用优雅降级替换部分内容；其他错误：保留部分内容供断点续传
                stream_err = result.pop("_stream_error", None)
                if stream_err:
                    if self._is_audit_error(stream_err):
                        logger.warning("call_model 流式触发内容审核 | conv=%s", conv_id)
                        return self._audit_fallback()
                    # 非审核错误：保留已流式的部分内容（stream.py 会保存到 DB 供续传）
                    logger.warning(
                        "call_model 流式异常但保留部分内容 | conv=%s | partial_len=%d | error=%s",
                        conv_id, len(result.get("full_response", "")), stream_err,
                    )
                return result
        except asyncio.TimeoutError:
            logger.error("call_model 超时 | conv=%s | model=%s", conv_id, model)
            raise
        except Exception as exc:
            if self._is_audit_error(exc):
                logger.warning("call_model 触发内容审核 | conv=%s", conv_id)
                return self._audit_fallback()
            logger.error("call_model LLM 异常 | conv=%s | model=%s | error=%s", conv_id, model, exc, exc_info=True)
            raise

    # _stream_tokens / _is_audit_error / _audit_fallback 继承自 BaseNode

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
