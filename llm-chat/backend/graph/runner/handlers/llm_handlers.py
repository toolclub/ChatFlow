"""
LLM 相关事件处理器

LLMStartHandler:             LLM 节点开始推理 → 推送 thinking 状态，重置流式标记
LLMStreamHandler:            LLM token 流 → 逐 token 转发，分离 <think> 块
CallModelEndHandler:         call_model 结束 → 未流式时补发完整内容
CallModelAfterToolEndHandler: call_model_after_tool 结束 → 未流式时补发完整内容

迁移说明：
  原版 LLMStartHandler 监听 on_chat_model_start（LangChain ChatOpenAI 触发）。
  迁移到原生 AsyncOpenAI 后，LangGraph 不再触发 on_chat_model_start，
  改为监听 on_chain_start + call_model/call_model_after_tool 节点，效果相同。
"""
import re
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers.base import EventHandler
from graph.runner.utils import sse

# 需要推送 thinking 状态的节点集合
_LLM_NODES = frozenset({"call_model", "call_model_after_tool"})

# MiniMax 等模型在流式模式下可能输出的工具调用文本残留（需过滤）
_TOOL_CALL_ARTIFACTS = ("<minimax:tool_call>", "[TOOL_CALL]")


class LLMStartHandler(EventHandler):
    """
    LLM 节点开始推理：通知前端进入 thinking 状态，重置流式标记。

    监听 on_chain_start（节点级别）而非 on_chat_model_start（LangChain 级别），
    兼容原生 AsyncOpenAI（不再触发 on_chat_model_start）。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and node_name in _LLM_NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        node = event.get("metadata", {}).get("langgraph_node", "")
        # 重置流式标记（每次节点启动前必须重置，防止上一轮状态污染）
        if node == "call_model_after_tool":
            ctx.after_tool_streamed = False
        elif node == "call_model":
            ctx.call_model_streamed = False
        yield sse({"status": "thinking", "model": ctx.active_model})


class LLMStreamHandler(EventHandler):
    """
    主推理节点 token 流：逐 token 发送增量内容，<think> 推理块以 thinking 事件推送。

    监听节点内通过 adispatch_custom_event("llm_token", ...) 派发的自定义事件，
    对应 LangGraph astream_events 的 on_custom_event 类型。

    <think> 块可能跨 chunk，ctx.in_think_block 跨 chunk 维持状态。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        # 监听 adispatch_custom_event 触发的 on_custom_event，名称为 llm_token
        return (
            event_type == "on_custom_event"
            and event_name == "llm_token"
            and node_name in _LLM_NODES
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        data = event.get("data", {})
        if not isinstance(data, dict):
            return

        content = data.get("content", "")
        if not content:
            return

        # ── 分离 <think>...</think> 推理块（可能跨 chunk） ───────────────────
        think_parts:  list[str] = []
        output_parts: list[str] = []
        pos = 0

        while pos < len(content):
            if ctx.in_think_block:
                end = content.find("</think>", pos)
                if end == -1:
                    think_parts.append(content[pos:])
                    pos = len(content)
                else:
                    think_parts.append(content[pos:end])
                    ctx.in_think_block = False
                    pos = end + len("</think>")
            else:
                start = content.find("<think>", pos)
                if start == -1:
                    output_parts.append(content[pos:])
                    break
                output_parts.append(content[pos:start])
                ctx.in_think_block = True
                pos = start + len("<think>")

        thinking = "".join(think_parts)
        filtered = "".join(output_parts)

        # 过滤 MiniMax 等模型的工具调用残留文本
        if any(artifact in filtered for artifact in _TOOL_CALL_ARTIFACTS):
            filtered = ""

        node = event.get("metadata", {}).get("langgraph_node", "")

        if thinking:
            yield sse({"thinking": thinking})

        if filtered:
            # 标记已流式发送，CallModelEnd/AfterToolEndHandler 检测到后跳过重复推送
            if node == "call_model_after_tool":
                ctx.after_tool_streamed = True
            elif node == "call_model":
                ctx.call_model_streamed = True
            yield sse({"content": filtered})


class CallModelEndHandler(EventHandler):
    """
    call_model 节点结束处理器。

    使用原生 AsyncOpenAI 时，节点内直接完成 LLM 调用（非 LangChain 可观测流），
    on_chat_model_stream 不触发，ctx.call_model_streamed 始终为 False。
    此 handler 从 on_chain_end 的节点输出中读取 full_response 并推送。

    有工具调用时由 call_model_after_tool 负责内容，此处跳过。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and event_name == "call_model"
            and node_name == "call_model"
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        # 流式已发则跳过（兼容未来可能添加的自定义流式实现）
        if ctx.call_model_streamed:
            return

        output = event.get("data", {}).get("output", {})
        if not isinstance(output, dict):
            return

        # 有工具调用时由 call_model_after_tool 负责内容，跳过
        messages = output.get("messages", [])
        if messages:
            last = messages[-1]
            tool_calls = (
                getattr(last, "tool_calls", None)
                or (last.get("tool_calls") if isinstance(last, dict) else None)
            )
            if tool_calls:
                return

        full_response = output.get("full_response", "")
        if not full_response:
            return

        # 分离 think 块后推送
        think_match = re.search(r"<think>([\s\S]*?)</think>", full_response)
        if think_match:
            yield sse({"thinking": think_match.group(1).strip()})
        content = re.sub(r"<think>[\s\S]*?</think>", "", full_response).strip()
        if content:
            yield sse({"content": content})


class CallModelAfterToolEndHandler(EventHandler):
    """
    call_model_after_tool 节点结束处理器。

    与 CallModelEndHandler 逻辑类似：
    - 原生 AsyncOpenAI 调用时 ctx.after_tool_streamed 始终为 False
    - 从节点输出的 full_response 读取并推送完整内容
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and "call_model_after_tool" in (event_name, node_name)
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        # 流式已发则跳过
        if ctx.after_tool_streamed:
            return

        output = event.get("data", {}).get("output", {})
        full_response = output.get("full_response", "") if isinstance(output, dict) else ""
        if not full_response:
            return

        # 分离 think 块后推送
        think_match = re.search(r"<think>([\s\S]*?)</think>", full_response)
        if think_match:
            yield sse({"thinking": think_match.group(1).strip()})
        content = re.sub(r"<think>[\s\S]*?</think>", "", full_response).strip()
        if content:
            yield sse({"content": content})
