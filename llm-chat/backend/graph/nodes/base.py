import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar, cast
from functools import wraps

from langchain_core.messages import AIMessage, BaseMessage

from graph.state import GraphState, PlanStep

logger = logging.getLogger("graph.nodes.base")

T = TypeVar("T", bound=Callable[..., Any])

def track_usage(func: T) -> T:
    """
    装饰器：自动统计并持久化图节点的 Token 使用情况。
    
    逻辑：
      - 执行节点逻辑
      - 若返回字典中包含 "usage"，提取并记录到 DB
      - 支持区分登录用户 (user_id) 和游客 (client_id)
    """
    @wraps(func)
    async def wrapper(self: "BaseNode", state: GraphState, *args, **kwargs):
        result = await func(self, state, *args, **kwargs)
        
        if isinstance(result, dict) and "usage" in result:
            usage = result["usage"]
            if usage:
                user_id = state.get("user_id", "")
                client_id = state.get("client_id", "")
                conv_id = state.get("conv_id", "")
                node = self.name
                # 模型名优先从结果取（可能是 tool_model），其次从 state 取
                model = result.get("model") or state.get("tool_model") or state.get("model", "unknown")
                
                try:
                    from db.usage_store import record_usage
                    # record_usage 现在是异步的
                    await record_usage(
                        user_id=user_id,
                        client_id=client_id,
                        conv_id=conv_id,
                        node=node,
                        model=model,
                        usage=usage
                    )
                except (ImportError, ModuleNotFoundError) as e:
                    logger.error("Usage tracking import failed: %s", e)
                except Exception as e:
                    logger.error("Usage tracking recording failed: %s", e)
        return result
    return cast(T, wrapper)


class BaseNode(ABC):
    """
    所有图节点的抽象基类。

    子类需实现：
        name    → 节点名称字符串（对应 graph.add_node 第一参数）
        execute → 节点主逻辑，接收 GraphState，返回 dict 更新
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """节点名称，对应 graph.add_node(name, fn) 的 name 参数。"""
        ...

    @abstractmethod
    async def execute(self, state: GraphState) -> dict:
        """
        节点主逻辑。

        参数：
            state: 当前 GraphState
        返回：
            dict：要更新回 GraphState 的字段，由 LangGraph add_messages reducer 合并
        """
        ...

    # ══════════════════════════════════════════════════════════════════════════
    # 消息格式转换工具（供 call_model 系节点使用）
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _to_openai_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """
        将 LangChain BaseMessage 列表转为 OpenAI SDK 原生 dict 格式。

        HumanMessage.content 若已是 list（多模态），直接透传。
        AIMessage 中的 tool_calls 同步转换为 OpenAI function calling 格式，
        确保 call_model_after_tool 在含图片场景下能正确重放工具调用历史。
        """
        role_map = {
            "SystemMessage":   "system",
            "HumanMessage":    "user",
            "AIMessage":       "assistant",
            "ToolMessage":     "tool",
            "FunctionMessage": "function",
        }
        result: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            role = role_map.get(type(msg).__name__, "user")
            content = msg.content

            # AIMessage 中的 tool_calls 处理：DeepSeek 强制要求 tool_calls 必须紧跟 tool 响应
            oai_tool_calls = None
            if role == "assistant":
                lc_tool_calls = getattr(msg, "tool_calls", None) or []
                if lc_tool_calls:
                    # 检查后续是否紧跟 tool 响应
                    has_responses = False
                    if i + 1 < len(messages):
                        next_msg = messages[i+1]
                        # 只要紧跟了一个 ToolMessage，我们就认为这一组调用是有效的（简化判断）
                        if type(next_msg).__name__ in ("ToolMessage", "FunctionMessage"):
                            has_responses = True
                    
                    if has_responses:
                        oai_tool_calls = []
                        for tc in lc_tool_calls:
                            if isinstance(tc, dict):
                                tc_id   = tc.get("id", "")
                                tc_name = tc.get("name", "")
                                tc_args = tc.get("args", {})
                            else:
                                tc_id   = getattr(tc, "id", "")
                                tc_name = getattr(tc, "name", "")
                                tc_args = getattr(tc, "args", {})
                            oai_tool_calls.append({
                                "id": tc_id,
                                "type": "function",
                                "function": {
                                    "name": tc_name,
                                    "arguments": json.dumps(tc_args, ensure_ascii=False),
                                },
                            })
                        content = None  # 有 tool_calls 时 content 须为 None
                    else:
                        # 悬挂的 tool_calls：如果没有紧跟工具响应，DeepSeek 会报 400
                        # 业界的做法是剥离这些未完成的调用，保留文本内容
                        logger.warning("发现悬挂的 tool_calls (index=%d)，由于无后续响应已剥离以兼容 DeepSeek", i)
                        oai_tool_calls = None
                        if not content:
                            # 如果 content 为空，尝试从 tool_summary 恢复或给个占位符
                            summary = getattr(msg, "tool_summary", "")
                            content = f"[已中断的工具调用] {summary}" if summary else "..."

            entry: dict[str, Any] = {"role": role, "content": content}

            # ── 适配 DeepSeek 推理内容 ──────────────────────────────────
            if role == "assistant":
                reasoning = msg.additional_kwargs.get("reasoning_content")
                if reasoning:
                    entry["reasoning_content"] = reasoning
                if oai_tool_calls:
                    entry["tool_calls"] = oai_tool_calls

            # ToolMessage 需要带上 tool_call_id
            if role == "tool":
                entry["tool_call_id"] = getattr(msg, "tool_call_id", "")

            result.append(entry)
        return result

    @staticmethod
    def _tools_to_openai_schema(tools: list) -> list[dict[str, Any]]:
        """
        将 LangChain BaseTool 列表转为 OpenAI tools 参数格式。
        用于向 OpenAI SDK 传递工具定义（function calling）。
        """
        result = []
        for tool in tools:
            schema = getattr(tool, "args_schema", None)
            if schema is not None:
                parameters = (
                    schema.model_json_schema()
                    if hasattr(schema, "model_json_schema")
                    else schema.schema()
                )
                # 移除 pydantic 自动生成的 title，保持简洁
                parameters.pop("title", None)
            else:
                parameters = {"type": "object", "properties": {}}
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": getattr(tool, "description", ""),
                    "parameters": parameters,
                },
            })
        return result

    @staticmethod
    def _convert_oai_tool_calls(oai_tool_calls: list) -> list[dict[str, Any]]:
        """
        将 OpenAI ChatCompletionMessage.tool_calls 转为 LangChain AIMessage.tool_calls 格式。

        这样返回的 AIMessage 能被 LangGraph ToolNode 正确识别和执行。
        格式：[{"id": ..., "name": ..., "args": {...}, "type": "tool_call"}]
        """
        lc_tool_calls = []
        for tc in oai_tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {"_raw": tc.function.arguments}
            lc_tool_calls.append({
                "id":   tc.id,
                "name": tc.function.name,
                "args": args,
                "type": "tool_call",
            })
        return lc_tool_calls

    # ══════════════════════════════════════════════════════════════════════════
    # 计划步骤工具
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _mark_step(plan: list[PlanStep], idx: int, status: str) -> list[PlanStep]:
        """
        返回将指定步骤状态设为 status 的新计划列表（不修改原列表）。

        状态转换通过 PlanStepSM 状态机校验：pending→running→done|failed。
        非法转换记录警告并跳过（不崩溃，降级容错）。
        """
        updated = list(plan)
        if 0 <= idx < len(updated):
            current_status = updated[idx].get("status", "pending")
            # 已在目标状态（如 retry 时 running → running），跳过状态机直接返回
            if current_status == status:
                return updated
            from fsm.plan_step import PlanStepSM
            try:
                sm = PlanStepSM.from_db_status(current_status)
                new_status = sm.send_event(status)
            except Exception:
                # 状态机校验失败（如 done → running），降级直接设置
                import logging
                logging.getLogger("graph.nodes.base").warning(
                    "步骤状态转换异常: %s → %s (idx=%d)，降级直接设置",
                    current_status, status, idx,
                )
                new_status = status
            updated[idx] = {**updated[idx], "status": new_status}
        return updated

    # ══════════════════════════════════════════════════════════════════════════
    # 思考事件统一入口（spec.md「模型思考流程」协议）
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    async def emit_thinking(
        node: str,
        phase: str,
        delta: str,
        step_index: int | None = None,
    ) -> None:
        """
        统一推送 llm_thinking 事件（结构化 payload）。

        协议：{node, step_index, phase, delta}，所有节点调用此方法，
        禁止散落 adispatch_custom_event("llm_thinking", ...) 裸调用（spec 铁律 #1）。

        参数：
            node:        节点名（planner / route_model / call_model /
                         call_model_after_tool / reflector / vision）
            phase:       "reasoning"（推理链）| "content"（外显文本）
            delta:       增量文本，空串直接跳过
            step_index:  有计划时为 0-based 步骤索引；否则 None
        """
        if not delta:
            return
        from langchain_core.callbacks.manager import adispatch_custom_event
        await adispatch_custom_event(
            "llm_thinking",
            {
                "node": node,
                "step_index": step_index,
                "phase": phase,
                "delta": delta,
            },
        )

    @staticmethod
    def _active_step_index(state: dict | None) -> int | None:
        """从 GraphState 读当前步骤索引，无计划或未初始化时返回 None。"""
        if not state:
            return None
        plan = state.get("plan") or []
        if not plan:
            return None
        idx = state.get("current_step_index")
        if idx is None:
            return None
        if 0 <= idx < len(plan):
            return int(idx)
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # 流式 LLM 调用工具（供 CallModel 系节点共享）
    # ══════════════════════════════════════════════════════════════════════════

    # 推理模型（GLM/DeepSeek-R1）thinking token 前缀标识
    _THINK_PREFIX = "\x00THINK\x00"

    # 内容审核错误标识（MiniMax 等厂商特定错误码）
    _AUDIT_MARKERS = ("1027", "sensitive")

    @staticmethod
    async def _stream_tokens(
        llm,
        oai_messages: list,
        temperature: float,
        conv_id: str,
        node: str,
        timeout: float = 180.0,
        step_index: int | None = None,
    ) -> dict:
        """
        流式 LLM 调用：逐 token yield，通过 adispatch_custom_event 发出事件。

        LLMStreamHandler 在 astream_events 中监听这些事件，实时推送给前端。
        节点返回时 full_response 包含完整内容，CallModelEndHandler 因
        ctx.*_streamed=True 而跳过重复发送。

        异常安全：流式中途抛异常（如 MiniMax 审核 1027）时，
        已累积的 content_parts 不会丢失 —— 作为 partial full_response 返回，
        供 stream.py 的 _streaming[0] + _partial[0] 捕获并保存到 DB。

        支持推理模型（GLM/DeepSeek-R1 等）的 thinking token：
          - "\\x00THINK\\x00" 前缀的 delta 作为 thinking 事件分发
          - 普通 delta 作为 llm_token 事件分发
        """
        from langchain_core.callbacks.manager import adispatch_custom_event

        _THINK_PREFIX = BaseNode._THINK_PREFIX
        content_parts:  list[str] = []
        thinking_parts: list[str] = []
        usage_data:     dict = {}
        token_count = 0

        try:
            async for delta in llm.astream(oai_messages, temperature=temperature, timeout=timeout):
                if isinstance(delta, dict) and "usage" in delta:
                    usage_data = delta["usage"]
                    continue
                
                token_count += 1
                if delta.startswith(_THINK_PREFIX):
                    thinking_text = delta[len(_THINK_PREFIX):]
                    thinking_parts.append(thinking_text)
                    await BaseNode.emit_thinking(node, "reasoning", thinking_text, step_index)
                else:
                    content_parts.append(delta)
                    # content 通过 llm_token 走常规 content SSE（llm_handlers.py 内部剥离 <think> 后转发）
                    await adispatch_custom_event("llm_token", {"content": delta, "node": node, "step_index": step_index})
        except Exception as exc:
            # ── 流式中途异常（审核 1027 / 网络断开等）────────────────────────
            # 已累积的 token 不能丢！将 partial content 作为 full_response 返回，
            # 上层节点和 stream.py 可根据此内容做断点续传保存。
            partial = "".join(content_parts)
            if partial:
                logger.warning(
                    "_stream_tokens 中途异常，保留部分内容 | conv=%s | node=%s | "
                    "tokens=%d | partial_len=%d | error=%s",
                    conv_id, node, token_count, len(partial), exc,
                )
                return {
                    "messages":      [AIMessage(content=partial)],
                    "full_response": partial,
                    "_was_streamed": True,
                    "_stream_error": exc,  # 供上层节点判断是否需要特殊处理
                }
            # 没有任何内容时直接抛出，让上层处理
            raise

        full_content  = "".join(content_parts)
        full_thinking = "".join(thinking_parts)
        logger.info(
            "_stream_tokens 完成 | conv=%s | node=%s | tokens=%d | content_len=%d | thinking_len=%d",
            conv_id, node, token_count, len(full_content), len(full_thinking),
        )
        
        # 将 thinking 塞入 AIMessage 的 additional_kwargs，确保图执行期间后续节点能拿到
        additional_kwargs = {}
        if full_thinking:
            additional_kwargs["reasoning_content"] = full_thinking
            
        result: dict = {
            "messages":      [AIMessage(content=full_content, additional_kwargs=additional_kwargs)],
            "full_response": full_content,
            "_was_streamed": True,
            "usage":         usage_data,
        }
        if full_thinking:
            result["full_thinking"] = full_thinking
        return result

    @staticmethod
    async def _stream_tokens_with_tools(
        llm,
        oai_messages: list,
        tools_schema: list,
        temperature: float,
        conv_id: str,
        node: str,
        timeout: float = 180.0,
        step_index: int | None = None,
    ) -> dict:
        """
        流式工具调用：thinking/content 实时推送给前端，同时收集 tool_calls。

        解决的问题：ainvoke 绑定工具时前端 10-30 秒无任何反馈。
        现在模型思考过程（thinking）和中间文本（content）实时可见。
        """
        import json
        from langchain_core.callbacks.manager import adispatch_custom_event

        final_data = None

        try:
            async for kind, text in llm.astream_with_tools(
                oai_messages, tools=tools_schema, temperature=temperature, timeout=timeout,
            ):
                if kind == "content" and text:
                    await adispatch_custom_event("llm_token", {"content": text, "node": node, "step_index": step_index})
                elif kind == "thinking" and text:
                    await BaseNode.emit_thinking(node, "reasoning", text, step_index)
                elif kind == "tool_call_start" and text:
                    await adispatch_custom_event("tool_call_start", {"name": text, "node": node})
                elif kind == "tool_call_args" and text:
                    # 工具参数片段实时推送：前端在终端中展示"正在写代码"
                    await adispatch_custom_event("tool_call_args", {"text": text, "node": node})
                elif kind == "__done__":
                    final_data = json.loads(text)
        except Exception as exc:
            logger.warning(
                "_stream_tokens_with_tools 异常 | conv=%s | node=%s | error=%s",
                conv_id, node, exc,
            )
            raise

        if not final_data:
            return {"messages": [], "full_response": ""}

        content = final_data.get("content", "")
        thinking = final_data.get("thinking", "")
        tool_calls_raw = final_data.get("tool_calls", [])
        usage_data = final_data.get("usage", {})

        # 统一提取推理内容
        additional_kwargs = {}
        if thinking:
            additional_kwargs["reasoning_content"] = thinking

        if tool_calls_raw:
            # 有 tool_calls → 构建 LangChain AIMessage
            lc_tool_calls = []
            for tc in tool_calls_raw:
                try:
                    args = json.loads(tc["arguments"])
                except Exception:
                    args = {"_raw": tc["arguments"]}
                lc_tool_calls.append({
                    "id": tc["id"], "name": tc["name"],
                    "args": args, "type": "tool_call",
                })
            ai_msg = AIMessage(
                content=content, 
                tool_calls=lc_tool_calls,
                additional_kwargs=additional_kwargs
            )
            logger.info(
                "_stream_tokens_with_tools 完成（tool_calls）| conv=%s | node=%s | "
                "content_len=%d | tool_calls=%s | usage=%s",
                conv_id, node, len(content),
                [tc["name"] for tc in lc_tool_calls],
                usage_data,
            )
            result = {
                "messages": [ai_msg],
                "full_response": content,
                "_was_streamed": True,
                "usage": usage_data,
            }
            if thinking:
                result["full_thinking"] = thinking
            return result
        else:
            # 无 tool_calls → 纯文本回复
            ai_msg = AIMessage(content=content, additional_kwargs=additional_kwargs)
            logger.info(
                "_stream_tokens_with_tools 完成（纯文本）| conv=%s | node=%s | content_len=%d | usage=%s",
                conv_id, node, len(content), usage_data,
            )
            result = {
                "messages": [ai_msg],
                "full_response": content,
                "_was_streamed": True,
                "usage": usage_data,
            }
            if thinking:
                result["full_thinking"] = thinking
            return result

    @classmethod
    def _is_audit_error(cls, exc: Exception) -> bool:
        """判断是否为内容审核错误（MiniMax 1027 等）。"""
        err_str = str(exc)
        return any(marker in err_str for marker in cls._AUDIT_MARKERS)

    @staticmethod
    def _audit_fallback() -> dict:
        """内容审核错误时的优雅降级响应。"""
        msg = "抱歉，该内容触发了模型安全审核，无法生成回复。请换个方式描述问题。"
        return {"messages": [], "full_response": msg}
