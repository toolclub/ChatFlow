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
  {"thinking": "...chunk..."}                            ← <think> 推理块内容（增量，不存 DB）
  {"content": "...token..."}                             ← LLM 输出 token（增量）
  {"tool_call": {"name": "...", "input": {...}}}         ← 工具调用开始
  {"search_item": {"url":"","title":"","status":""}}     ← web_search 单条结果
  {"tool_result": {"name": "...", ...}}                  ← 工具完成信号
  {"ping": true}                                         ← 心跳（防 nginx 超时）
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
from graph.event_types import (
    CacheHitEndEvent,
    CompressEndEvent,
    LLMStreamEvent,
    PlannerEndEvent,
    ReflectorEndEvent,
    RouteEndEvent,
    ToolEndEvent,
    ToolStartEvent,
)
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
    # <think> 块过滤状态（qwen3 等模型的推理内容跨 chunk）
    in_think_block: bool = False
    # call_model_after_tool 当前轮次是否已通过流式发送过 token
    after_tool_streamed: bool = False
    # call_model 当前轮次是否已通过流式发送过 token（chat/code 路由 streaming=True）
    # 用于防止 CallModelEndHandler 在流式已发的情况下重复补发
    call_model_streamed: bool = False


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


class CacheHitEndHandler(EventHandler):
    """缓存命中：推送 cache_hit 状态 + 完整答案（复用 content 格式，前端无需改动）"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and "semantic_cache_check" in (event_name, node_name)
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = CacheHitEndEvent.from_event(event)
        if not ev.cache_hit:
            return
        yield _sse({"status": "cache_hit", "similarity": round(ev.cache_similarity, 4)})
        if ev.full_response:
            yield _sse({"content": ev.full_response})


class RouteStartHandler(EventHandler):
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "routing"})


class RouteEndHandler(EventHandler):
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = RouteEndEvent.from_event(event)
        if ev is None:
            return
        ctx.active_model = ev.answer_model or ctx.active_model
        intent = ev.route or _MODEL_TO_INTENT.get(ctx.active_model, "chat")
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
        ev = PlannerEndEvent.from_event(event)
        if ev is None or not ev.plan:
            return
        ctx.last_plan_step_count = len(ev.plan)
        steps = list(ev.plan)
        yield _sse({"plan_generated": {"steps": steps}})


class ReflectorEndHandler(EventHandler):
    """反思节点结束：推送步骤状态更新和反思结果"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and "reflector" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = ReflectorEndEvent.from_event(event)
        if ev is None:
            return
        if ev.plan:
            steps = list(ev.plan)
            yield _sse({"plan_generated": {"steps": steps}})
        if ev.reflection or ev.reflector_decision:
            yield _sse({"reflection": {"content": ev.reflection, "decision": ev.reflector_decision}})


class LLMStartHandler(EventHandler):
    """主推理节点 LLM 开始：通知前端进入 thinking 状态，重置流式标记"""

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_start" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        node = event.get("metadata", {}).get("langgraph_node", "")
        if node == "call_model_after_tool":
            ctx.after_tool_streamed = False
        elif node == "call_model":
            ctx.call_model_streamed = False
        yield _sse({"status": "thinking", "model": ctx.active_model})


class LLMStreamHandler(EventHandler):
    """主推理节点 token 流：逐 token 发送增量内容，<think> 推理块以 thinking 事件推送"""

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_stream" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = LLMStreamEvent.from_event(event)
        if ev is None:
            return
        # 分离 <think>...</think> 推理块（可能跨 chunk）
        think_parts: list[str] = []
        output_parts: list[str] = []
        pos = 0
        content = ev.content
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

        # 过滤 MiniMax streaming 模式下输出的 tool_call 文本残留
        if "<minimax:tool_call>" in filtered or "[TOOL_CALL]" in filtered:
            filtered = ""

        node = event.get("metadata", {}).get("langgraph_node", "")

        if thinking:
            yield _sse({"thinking": thinking})

        if filtered:
            if node == "call_model_after_tool":
                ctx.after_tool_streamed = True
            elif node == "call_model":
                ctx.call_model_streamed = True
            yield _sse({"content": filtered})


class CallModelEndHandler(EventHandler):
    """call_model 节点结束：
    - chat/code 路由：streaming=True，token 已由 LLMStreamHandler 发出 → 跳过。
    - search 路由且模型直接返回内容（无工具调用）：streaming=False，此处补发内容。
    - search 路由且有工具调用：call_model_after_tool 负责后续内容 → 跳过。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and event_name == "call_model"
            and node_name == "call_model"
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        if ctx.call_model_streamed:
            return  # 已流式发送，不重复

        output = event.get("data", {}).get("output", {})
        if not isinstance(output, dict):
            return

        # 有工具调用时由 call_model_after_tool 负责内容
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

        import re
        think_match = re.search(r"<think>([\s\S]*?)</think>", full_response)
        if think_match:
            yield _sse({"thinking": think_match.group(1).strip()})
        content = re.sub(r"<think>[\s\S]*?</think>", "", full_response).strip()
        if content:
            yield _sse({"content": content})


class CallModelAfterToolEndHandler(EventHandler):
    """call_model_after_tool 节点结束：
    流式成功时 tokens 已由 LLMStreamHandler 发出，此处跳过。
    流式失败降级为非流式时，从 on_chain_end 的 full_response 补发完整内容。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return (
            event_type == "on_chain_end"
            and "call_model_after_tool" in (event_name, node_name)
        )

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        if ctx.after_tool_streamed:
            return  # 流式已发，不重复
        # 非流式降级：从节点输出取 full_response 补发
        output = event.get("data", {}).get("output", {})
        full_response = output.get("full_response", "") if isinstance(output, dict) else ""
        if full_response:
            import re
            think_match = re.search(r"<think>([\s\S]*?)</think>", full_response)
            if think_match:
                yield _sse({"thinking": think_match.group(1).strip()})
            content = re.sub(r"<think>[\s\S]*?</think>", "", full_response).strip()
            if content:
                yield _sse({"content": content})

class ToolStartHandler(EventHandler):
    """工具调用开始：向前端推送工具名和参数"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_start"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = ToolStartEvent.from_event(event)
        yield _sse({"tool_call": {"name": ev.name, "input": ev.input}})


class ToolEndHandler(EventHandler):
    """工具调用完成：委托格式化策略推送结果"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_end"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = ToolEndEvent.from_event(event)
        formatter = _TOOL_FORMATTERS.get(ev.name, _DEFAULT_FORMATTER)
        async for chunk in formatter.format(ev.name, ev.raw_output):
            yield chunk


class SaveResponseEndHandler(EventHandler):
    """save_response 完成：通知前端进入保存阶段，防止后处理期间静默导致前端误判断流结束"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "save_response"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "saving"})


class CompressEndHandler(EventHandler):
    """记忆压缩完成：更新 ctx.compressed，不产生 SSE 输出"""

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "compress_memory"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        ev = CompressEndEvent.from_event(event)
        ctx.compressed = ev.compressed
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
    CacheHitEndHandler(),
    RouteStartHandler(),
    RouteEndHandler(),
    PlannerStartHandler(),
    PlannerEndHandler(),
    ReflectorEndHandler(),
    CallModelEndHandler(),            # call_model 无工具调用时补发内容
    CallModelAfterToolEndHandler(),   # call_model_after_tool 非流式降级时补发内容
    LLMStartHandler(),
    LLMStreamHandler(),
    ToolStartHandler(),
    ToolEndHandler(),
    SaveResponseEndHandler(),
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
    client_id: str = "",
    images: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """
    驱动 LangGraph 图执行，将事件流翻译为 FastAPI SSE 字符串流。

    设计要点：
      - 图执行在独立 Task 中运行，主循环通过 asyncio.Queue 接收事件
      - 心跳任务每 20s 向队列注入 ping，主循环 yield 给前端，防止 nginx 超时断流
      - recursion_limit=120：支持最多约 28 个计划步骤（每步 4 节点 + 固定开销 5）
      - 图 Task 取消时（客户端断开）主循环捕获 CancelledError 并退出，不发 done
    """
    graph = get_graph(model)
    ctx = StreamContext(active_model=model)

    initial_state: GraphState = {
        "conv_id": conv_id,
        "client_id": client_id,
        "user_message": user_message,
        "images": images or [],
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
        # 语义缓存初始值
        "cache_hit": False,
        # 认知规划初始值
        "plan": [],
        "current_step_index": 0,
        "step_iterations": 0,
        "reflector_decision": "",
        "reflection": "",
    }

    # ── 后台任务：驱动图执行，把事件塞进队列 ─────────────────────────────────
    queue: asyncio.Queue[tuple] = asyncio.Queue()

    async def _graph_producer() -> None:
        event_count = 0
        try:
            async for event in graph.astream_events(
                initial_state,
                version="v2",
                config={"recursion_limit": 120},
            ):
                event_count += 1
                etype = event.get("event", "")
                ename = event.get("name", "")
                enode = event.get("metadata", {}).get("langgraph_node", "")
                # 只记录关键节点事件，避免日志爆炸
                if etype in ("on_chain_start", "on_chain_end", "on_tool_start", "on_tool_end",
                             "on_chat_model_start", "on_chat_model_end"):
                    logger.info(
                        "图事件 #%d | conv=%s | event=%s | node=%s | name=%s",
                        event_count, conv_id, etype, enode, ename,
                    )
                await queue.put(("event", event))
        except asyncio.CancelledError:
            logger.info("图执行被取消（客户端断开） | conv=%s | events=%d", conv_id, event_count)
            pass
        except Exception as exc:
            logger.error("图执行失败 | conv=%s | events=%d | error=%s", conv_id, event_count, exc, exc_info=True)
            try:
                await queue.put(("error", exc))
            except Exception:
                pass
        finally:
            logger.info("图执行结束 | conv=%s | total_events=%d", conv_id, event_count)
            try:
                await queue.put(("done", None))
            except Exception:
                pass

    # ── 后台任务：每 20s 发一次心跳，防止 nginx proxy_read_timeout 断流 ──────
    async def _heartbeat() -> None:
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(5)
                ping_count += 1
                logger.info("心跳发送 #%d | conv=%s", ping_count, conv_id)
                await queue.put(("ping", None))
        except asyncio.CancelledError:
            logger.info("心跳任务结束 | conv=%s | pings=%d", conv_id, ping_count)
            pass

    graph_task = asyncio.create_task(_graph_producer())
    hb_task    = asyncio.create_task(_heartbeat())

    logger.info("SSE 流开始 | conv=%s | model=%s | message='%.60s'", conv_id, model, user_message)
    graph_done = False
    try:
        while True:
            kind, _event = await queue.get()
            if kind == "event":
                async for chunk in _dispatcher.dispatch(_event, ctx):
                    yield chunk
            elif kind == "ping":
                logger.info("心跳已发送到前端 | conv=%s", conv_id)
                yield _sse({"ping": True})
            elif kind == "error":
                logger.error("SSE 收到图错误 | conv=%s | error=%s", conv_id, str(_event))
                yield _sse({"error": str(_event)})
                # 继续等待 done 信号，确保 finally 正常执行
            elif kind == "done":
                graph_done = True
                break
    except asyncio.CancelledError:
        logger.info("SSE 连接已断开（客户端关闭） | conv=%s", conv_id)
        graph_done = False
    finally:
        hb_task.cancel()
        graph_task.cancel()
        logger.info("SSE 流结束 | conv=%s | graph_done=%s", conv_id, graph_done)

    if graph_done:
        yield _sse({"done": True, "compressed": ctx.compressed})
