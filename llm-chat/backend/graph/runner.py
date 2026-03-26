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
from dataclasses import dataclass
from typing import AsyncGenerator, ClassVar

from config import ROUTE_MODEL_MAP
from graph.agent import get_graph
from graph.state import GraphState

logger = logging.getLogger("graph.runner")

# model → intent 反查表（路由结束时向前端汇报意图标签）
_MODEL_TO_INTENT: dict[str, str] = {v: k for k, v in ROUTE_MODEL_MAP.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 会话上下文（单次 stream_response 生命周期内共享）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StreamContext:
    """
    单次 stream_response 调用的可变共享状态。
    由 EventDispatcher 创建并传入每个 handler，handler 可读写其字段。

    使用场景：所有 EventHandler 子类在处理事件时读写此上下文。
    字段：
      active_model  —— 当前生效的推理模型名（路由结束后由 RouteEndHandler 更新）
      compressed    —— 本轮是否触发了记忆压缩（由 CompressEndHandler 写入）
    """
    active_model: str
    compressed: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# 工具结果格式化策略
# ══════════════════════════════════════════════════════════════════════════════

class ToolResultFormatter(ABC):
    """
    工具结果格式化策略基类（策略模式）。

    每个子类负责将某类工具的原始输出转换为若干条 SSE 字符串。
    ToolEndHandler 通过工具名在 _TOOL_FORMATTERS 注册表中查找对应策略，
    未注册的工具自动落到 GenericToolFormatter 兜底，满足开闭原则。

    使用场景：on_tool_end 事件中，ToolEndHandler 委托给具体子类格式化输出。
    """

    @abstractmethod
    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        """将工具原始输出转换为若干条 SSE 行（含 data: 前缀和双换行）。"""
        ...


class WebSearchFormatter(ToolResultFormatter):
    """
    web_search 工具结果格式化。

    将 JSON 数组逐条解析为 search_item 事件，使前端实时追加 URL 卡片，
    最后发送一条不含内容的 tool_result 完成信号（内容已通过 search_item 传递）。

    使用场景：on_tool_end 事件，工具名为 "web_search"。
    """

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
    """
    fetch_webpage 工具结果格式化。

    前端只需成功/失败状态（完整页面内容已传给 LLM，不需要前端展示）。
    通过识别特定前缀字符串判断读取是否失败。

    使用场景：on_tool_end 事件，工具名为 "fetch_webpage"。
    """

    _FAIL_PREFIXES: ClassVar[tuple[str, ...]] = ("读取超时", "HTTP 错误", "读取失败")

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        status = "fail" if any(raw.startswith(p) for p in self._FAIL_PREFIXES) else "done"
        yield _sse({"tool_result": {"name": name, "status": status}})


class GenericToolFormatter(ToolResultFormatter):
    """
    通用工具结果格式化（兜底策略）。

    将原始输出截断到 1000 字符后作为 output 字段返回，
    适用于 calculator、get_current_time 及所有未单独注册的工具。

    使用场景：on_tool_end 事件，工具名在 _TOOL_FORMATTERS 中未找到时。
    """

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        yield _sse({"tool_result": {"name": name, "output": raw[:1000]}})


# 工具名 → 格式化策略注册表（新增工具只需在此添加，无需修改任何 handler）
_TOOL_FORMATTERS: dict[str, ToolResultFormatter] = {
    "web_search":    WebSearchFormatter(),
    "fetch_webpage": FetchWebpageFormatter(),
}
_DEFAULT_FORMATTER = GenericToolFormatter()


# ══════════════════════════════════════════════════════════════════════════════
# 事件处理器
# ══════════════════════════════════════════════════════════════════════════════

class EventHandler(ABC):
    """
    LangGraph astream_events 事件处理器基类。

    子类通过 matches() 声明自己感兴趣的事件，
    通过 handle() 将事件翻译为 SSE 字符串序列（可 yield 零条或多条）。

    EventDispatcher 遍历已注册的 handler 列表，
    第一个 matches() 返回 True 的 handler 负责处理该事件，后续 handler 跳过。
    """

    @abstractmethod
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        """声明此 handler 是否处理该事件。"""
        ...

    @abstractmethod
    async def handle(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        """将事件翻译为若干条 SSE 字符串（含 data: 前缀和双换行）。"""
        ...


class RouteStartHandler(EventHandler):
    """
    处理路由节点启动事件（on_chain_start @ route_model）。

    向前端发送 status=routing，触发「意图分类中」加载动画。

    使用场景：config.ROUTER_ENABLED=True 且路由节点开始运行时。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_start" and "route_model" in (event_name, node_name)

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "routing"})


class RouteEndHandler(EventHandler):
    """
    处理路由节点完成事件（on_chain_end @ route_model）。

    解析路由输出，更新 ctx.active_model，并向前端发送路由结果
    （含选定模型名和 intent 标签，前端据此更新状态栏显示）。

    使用场景：config.ROUTER_ENABLED=True 且路由节点执行完毕时。
    """

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
        # 优先直接读路由标签，避免同一模型对应多个路由时反查出错（如 search_code / code 同用代码模型）
        intent = output.get("route") or _MODEL_TO_INTENT.get(ctx.active_model, "chat")
        yield _sse({"route": {"model": ctx.active_model, "intent": intent}})


class LLMStartHandler(EventHandler):
    """
    处理 LLM 推理开始事件（on_chat_model_start @ call_model 系列节点）。

    向前端发送 status=thinking + 当前模型名，触发「思考中」动画。
    仅响应主推理节点，忽略 compress_memory 内部摘要模型产生的同类事件。

    使用场景：call_model 或 call_model_after_tool 节点开始调用 LLM 时。
    """

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_start" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        yield _sse({"status": "thinking", "model": ctx.active_model})


class LLMStreamHandler(EventHandler):
    """
    处理 LLM token 流事件（on_chat_model_stream @ call_model 系列节点）。

    逐 token 向前端发送 content 增量，实现流式打字效果。
    仅响应主推理节点，忽略 compress_memory 内部摘要 token。

    使用场景：call_model 或 call_model_after_tool 节点流式输出 token 时。
    """

    _NODES: ClassVar[frozenset[str]] = frozenset({"call_model", "call_model_after_tool"})

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chat_model_stream" and node_name in self._NODES

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        chunk = event["data"].get("chunk")
        if chunk and chunk.content:
            yield _sse({"content": chunk.content})


class ToolStartHandler(EventHandler):
    """
    处理工具调用开始事件（on_tool_start，任意工具）。

    向前端发送 tool_call 事件（含工具名和输入参数），
    前端据此在消息气泡中插入工具调用卡片并显示「执行中」状态。

    使用场景：LLM 决定调用任意工具时（web_search、fetch_webpage、calculator 等）。
    """

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
    """
    处理工具调用完成事件（on_tool_end，任意工具）。

    1. 通过 _extract_tool_output() 从 ToolMessage 或裸字符串中提取原始内容
    2. 在 _TOOL_FORMATTERS 注册表中查找工具名对应的格式化策略
    3. 未注册的工具使用 GenericToolFormatter 兜底

    新增工具只需在 _TOOL_FORMATTERS 中注册，此 handler 无需修改。

    使用场景：任意工具执行完毕，结果已写入 LLM 消息列表后。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_tool_end"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        name = event.get("name", "")
        raw = _extract_tool_output(event["data"].get("output", ""))
        formatter = _TOOL_FORMATTERS.get(name, _DEFAULT_FORMATTER)
        async for chunk in formatter.format(name, raw):
            yield chunk


class CompressEndHandler(EventHandler):
    """
    处理记忆压缩节点完成事件（on_chain_end @ compress_memory）。

    将压缩结果写入 ctx.compressed，该值随最终 done 信号一起发送给前端。
    本 handler 不产生任何 SSE 输出，仅更新共享状态。

    使用场景：对话轮数超过 COMPRESS_TRIGGER 阈值，compress_memory 节点执行完毕后。
    """

    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        return event_type == "on_chain_end" and event_name == "compress_memory"

    async def handle(self, event: dict, ctx: StreamContext) -> AsyncGenerator[str, None]:
        output = event["data"].get("output", {})
        if isinstance(output, dict):
            ctx.compressed = output.get("compressed", False)
        return
        yield  # 声明此函数为 AsyncGenerator 但不产生任何 SSE 输出


# ══════════════════════════════════════════════════════════════════════════════
# 事件派发器
# ══════════════════════════════════════════════════════════════════════════════

class EventDispatcher:
    """
    LangGraph 事件派发器（责任链模式）。

    持有有序的 EventHandler 列表，对每个事件依次尝试 matches()，
    第一个命中的 handler 执行 handle() 并 yield 其产生的 SSE 字符串，后续跳过。
    新增事件类型只需注册新 handler，派发逻辑本身无需改动（开闭原则）。

    使用场景：stream_response 函数的事件循环替代原有 if/elif 链。
    """

    def __init__(self, handlers: list[EventHandler]) -> None:
        self._handlers = handlers

    async def dispatch(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        """将事件派发给第一个匹配的 handler，yield 其产生的 SSE 字符串。"""
        event_type: str = event["event"]
        node_name: str = event.get("metadata", {}).get("langgraph_node", "")
        event_name: str = event.get("name", "")

        for handler in self._handlers:
            if handler.matches(event_type, node_name, event_name):
                async for chunk in handler.handle(event, ctx):
                    yield chunk
                return


# 全局派发器单例（应用启动时初始化一次，所有请求复用）
_dispatcher = EventDispatcher([
    RouteStartHandler(),
    RouteEndHandler(),
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
    """
    将字典序列化为标准 SSE 行：``data: {...}\\n\\n``。

    使用场景：所有 EventHandler.handle() 和 ToolResultFormatter.format() 内部统一调用。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_tool_output(output: object) -> str:
    """
    从 LangGraph on_tool_end 事件的 output 字段提取原始字符串。

    新版 LangGraph ToolNode 将工具返回值包装为 ToolMessage 对象，
    需取 .content 属性；旧版或直接返回字符串时则直接转换。

    使用场景：ToolEndHandler.handle() 预处理工具输出，统一为 str 类型。
    """
    if hasattr(output, "content"):
        content = output.content
        return content if isinstance(content, str) else str(content)
    return str(output)


# ══════════════════════════════════════════════════════════════════════════════
# 公开接口（main.py 唯一调用入口，签名不变）
# ══════════════════════════════════════════════════════════════════════════════

async def stream_response(
    conv_id: str,
    user_message: str,
    model: str,
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """
    驱动 LangGraph 图执行，将事件流翻译为 FastAPI SSE 字符串流。

    调用方（main.py /api/chat 接口）直接 async for 消费本函数输出：
        async for chunk in graph_runner.stream_response(...):
            yield chunk

    异常处理：
      asyncio.CancelledError —— 客户端主动断开连接，静默退出不报错。
      其他异常              —— 记录错误日志，向前端发送 error 事件后结束。

    使用场景：前端每次发送消息时由 /api/chat 路由调用一次。
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
        "tool_model": model,    # route_model 会覆盖；ROUTER_ENABLED=False 时用前端选的模型
        "answer_model": model,  # 同上
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
