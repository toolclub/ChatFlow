"""
图运行器：将 LangGraph astream_events 翻译为 FastAPI SSE 字符串流

架构概览：
  StreamContext          —— 会话级可变状态（active_model / compressed）
  EventHandler (ABC)     —— 事件处理器基类，子类各自负责一类 LangGraph 事件
  ToolResultFormatter    —— 工具结果格式化策略，按工具名注册，支持热扩展
  EventDispatcher        —— 持有全部 handler，顺序匹配后派发，无 if/else

SSE 事件格式（供前端消费）：
  {"status": "routing"}                                  ← 路由意图分类中
  {"route": {"model": "...", "intent": "..."}}           ← 路由结果
  {"status": "planning"}                                 ← 规划中
  {"plan_generated": {"steps": [...]}}                   ← 计划生成完毕
  {"step_update": {"index": N, "status": "running|done|failed"}} ← 步骤状态变化
  {"reflection": {"content": "...", "decision": "..."}}  ← 反思结果
  {"status": "thinking", "model": "..."}                 ← LLM 开始推理
  {"content": "...token..."}                             ← LLM 输出 token（增量）
  {"tool_call": {"name": "...", "input": {...}}}         ← 工具调用开始
  {"search_item": {"url":"","title":"","status":""}}     ← web_search 单条结果
  {"tool_result": {"name": "...", ...}}                  ← 工具完成信号
  {"done": true, "compressed": bool}                    ← 流结束信号

对外接口（main.py 唯一依赖）：
  stream_response(conv_id, user_message, model, temperature) -> AsyncGenerator[str, None]
"""
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, ClassVar

from config import ROUTE_MODEL_MAP
from graph.agent import get_graph
from graph.state import GraphState

logger = logging.getLogger("graph.runner")

_MODEL_TO_INTENT: dict[str, str] = {v: k for k, v in ROUTE_MODEL_MAP.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 会话上下文
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StreamContext:
    active_model: str
    compressed: bool = False
    # 最近一次计划（用于 step_update 事件去重）
    last_plan_step_count: int = 0


# ══════════════════════════════════════════════════════════════════════════════
# 工具结果格式化策略
# ══════════════════════════════════════════════════════════════════════════════

class ToolResultFormatter(ABC):
    @abstractmethod
    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        ...


class WebSearchFormatter(ToolResultFormatter):
    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        try:
            results = json.loads(raw)
            for item in results:
                url = item.get("url", "")
                yield _sse({
                    "search_item": {
                        "url": url,
                        "title": item.get("title", ""),
                        "status": "done" if url else "fail",
                    }
                })
        except Exception:
            pass
        yield _sse({"tool_result": {"name": name}})


class FetchWebpageFormatter(ToolResultFormatter):
    _FAIL_PREFIXES: ClassVar[tuple[str, ...]] = ("读取超时", "HTTP 错误", "读取失败")

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        status = "fail" if any(raw.startswith(p) for p in self._FAIL_PREFIXES) else "done"
        yield _sse({"tool_result": {"name": name, "status": status}})


class GenericToolFormatter(ToolResultFormatter):
    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        yield _sse({"tool_result": {"name": name, "output": raw[:1000]}})


_TOOL_FORMATTERS: dict[str, ToolResultFormatter] = {
    "web_search":    WebSearchFormatter(),
    "fetch_webpage": FetchWebpageFormatter(),
}
_DEFAULT_FORMATTER = GenericToolFormatter()


# ══════════════════════════════════════════════════════════════════════════════
# 事件处理器
# ══════════════════════════════════════════════════════════════════════════════

class EventHandler(ABC):
    @abstractmethod
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        ...

    @abstractmethod
    async def handle(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        ...


class RouteStartHandler(EventHandler):
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "routing"})


class RouteEndHandler(EventHandler):
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        output = event["data"].get("output", {})
        if not isinstance(output, dict):
            return
        ctx.active_model = (
            output.get("answer_model")
            or output.get("model")
            or ctx.active_model
        )
        intent = output.get("route") or _MODEL_TO_INTENT.get(ctx.active_model, "chat")
        yield _sse({"route": {"model": ctx.active_model, "intent": intent}})


class PlannerStartHandler(EventHandler):
    """规划节点启动：通知前端进入 planning 状态"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "planner" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "planning"})


class PlannerEndHandler(EventHandler):
    """规划节点结束：将生成的计划逐步推送给前端（模拟打字机效果由前端动画实现）"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "planner" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        output = event["data"].get("output", {})
        if not isinstance(output, dict):
            return
        plan = output.get("plan", [])
        if not plan:
            return
        ctx.last_plan_step_count = len(plan)
        yield _sse({"plan_generated": {"steps": plan}})


class ReflectorEndHandler(EventHandler):
    """反思节点结束：推送步骤状态更新和反思结果"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "reflector" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        output = event["data"].get("output", {})
        if not isinstance(output, dict):
            return

        # 推送更新后的计划（步骤状态变化）
        plan = output.get("plan", [])
        if plan:
            yield _sse({"plan_generated": {"steps": plan}})

        # 推送反思结果
        reflection = output.get("reflection", "")
        decision = output.get("reflector_decision", "")
        if reflection or decision:
            yield _sse({"reflection": {"content": reflection, "decision": decision}})


class LLMStartHandler(EventHandler):
    """主推理节点 LLM 开始：通知前端进入 thinking 状态"""

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_start" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "thinking", "model": ctx.active_model})


class LLMStreamHandler(EventHandler):
    """主推理节点 token 流：逐 token 发送增量内容"""

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_stream" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        chunk = event["data"].get("chunk")
        if chunk and chunk.content:
            yield _sse({"content": chunk.content})


class ToolStartHandler(EventHandler):
    """工具调用开始：向前端推送工具名和参数"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_start"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({
            "tool_call": {
                "name": event.get("name", ""),
                "input": event["data"].get("input", {}),
            }
        })


class ToolEndHandler(EventHandler):
    """工具调用完成：委托格式化策略推送结果"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_end"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        name = event.get("name", "")
        raw = _extract_tool_output(event["data"].get("output", ""))
        formatter = _TOOL_FORMATTERS.get(name, _DEFAULT_FORMATTER)
        async for chunk in formatter.format(name, raw):
            yield chunk


class CompressEndHandler(EventHandler):
    """记忆压缩完成：更新 ctx.compressed，不产生 SSE 输出"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "compress_memory"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        output = event["data"].get("output", {})
        if isinstance(output, dict):
            ctx.compressed = output.get("compressed", False)
        return
        yield  # 声明为 AsyncGenerator


# ══════════════════════════════════════════════════════════════════════════════
# 事件派发器
# ══════════════════════════════════════════════════════════════════════════════

class EventDispatcher:
    def __init__(self, handlers: list[EventHandler]) -> None:
        self._handlers = handlers

    async def dispatch(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        event_type: str = event["event"]
        node_name: str = event.get("metadata", {}).get("langgraph_node", "")
        event_name: str = event.get("name", "")

        for handler in self._handlers:
            if handler.matches(event_type, node_name, event_name):
                async for chunk in handler.handle(event, ctx):
                    yield chunk
                return


_dispatcher = EventDispatcher([
    RouteStartHandler(),
    RouteEndHandler(),
    PlannerStartHandler(),
    PlannerEndHandler(),
    ReflectorEndHandler(),
    LLMStartHandler(),
    LLMStreamHandler(),
    ToolStartHandler(),
    ToolEndHandler(),
    CompressEndHandler(),
])


# ══════════════════════════════════════════════════════════════════════════════
# 内部工具函数
# ══════════════════════════════════════════════════════════════════════════════

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_tool_output(output: object) -> str:
    if hasattr(output, "content"):
        content = output.content
        return content if isinstance(content, str) else str(content)
    return str(output)


# ══════════════════════════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════════════════════════

async def stream_response(
    conv_id: str,
    user_message: str,
    model: str,
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """
    驱动 LangGraph 图执行，将事件流翻译为 FastAPI SSE 字符串流。
    """
    graph = get_graph(model)
    ctx = StreamContext(active_model=model)

    initial_state: GraphState = {
        "conv_id": conv_id,
        "user_message": user_message,
        "model": model,
        "temperature": temperature,
        "messages": [],
        "long_term_memories": [],
        "forget_mode": False,
        "full_response": "",
        "compressed": False,
        "route": "",
        "tool_model": model,
        "answer_model": model,
        # 认知规划初始值
        "plan": [],
        "current_step_index": 0,
        "step_iterations": 0,
        "reflector_decision": "",
        "reflection": "",
    }

    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            async for chunk in _dispatcher.dispatch(event, ctx):
                yield chunk
    except asyncio.CancelledError:
        logger.info("SSE 连接已断开 conv=%s", conv_id)
        return
    except Exception as exc:
        logger.error("图执行失败 conv=%s: %s", conv_id, exc, exc_info=True)
        yield _sse({"error": str(exc)})

    yield _sse({"done": True, "compressed": ctx.compressed})
