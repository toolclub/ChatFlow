"""
EventDispatcher：事件派发器

持有全部 EventHandler，顺序遍历匹配后派发，无 if/else。

设计要点：
  - 职责链模式：每个 handler 只处理自己关注的事件类型
  - 首次匹配即处理，不继续传递（匹配成功后 return）
  - handler 注册顺序即优先级：高优先级的 handler 放前面
  - 不匹配任何 handler 的事件静默丢弃

扩展方式：
  1. 实现新的 EventHandler 子类
  2. 在下方 _HANDLERS 列表中按优先级插入
"""
from typing import AsyncGenerator

from graph.runner.context import StreamContext
from graph.runner.handlers import (
    CacheHitEndHandler,
    CallModelAfterToolEndHandler,
    CallModelEndHandler,
    ClarificationHandler,
    CompressEndHandler,
    EventHandler,
    LLMStartHandler,
    LLMStreamHandler,
    PlannerEndHandler,
    PlannerStartHandler,
    ReflectorEndHandler,
    RouteEndHandler,
    RouteStartHandler,
    SaveResponseEndHandler,
    ToolEndHandler,
    ToolStartHandler,
)

# ── Handler 注册顺序即优先级 ─────────────────────────────────────────────────
# LLMStreamHandler 监听 on_custom_event(llm_token)，在节点执行中逐 token 触发。
# CallModelEndHandler 监听 on_chain_end，检查 ctx.*_streamed 标志决定是否补发内容：
#   - 流式路径（无工具）：LLMStreamHandler 已发送，EndHandler 跳过
#   - 非流式路径（有工具调用）：LLMStreamHandler 不触发，EndHandler 从 full_response 补发
_HANDLERS: list[EventHandler] = [
    ClarificationHandler(),          # 澄清问询事件（on_custom_event）—— 优先处理
    CacheHitEndHandler(),
    RouteStartHandler(),
    RouteEndHandler(),
    PlannerStartHandler(),
    PlannerEndHandler(),
    ReflectorEndHandler(),
    CallModelEndHandler(),              # call_model 非流式时从 on_chain_end 补发内容
    CallModelAfterToolEndHandler(),     # call_model_after_tool 非流式时补发内容
    LLMStartHandler(),                  # 监听 on_chain_start → 推送 thinking 状态
    LLMStreamHandler(),                 # 监听 on_custom_event(llm_token) → 逐 token 推流
    ToolStartHandler(),
    ToolEndHandler(),
    SaveResponseEndHandler(),
    CompressEndHandler(),
]


class EventDispatcher:
    """
    事件派发器：将 LangGraph astream_events 事件路由到对应的 EventHandler。

    无状态：所有状态由传入的 StreamContext 维护。
    可复用：每个 stream_response 调用使用同一个 _dispatcher 单例。
    """

    def __init__(self, handlers: list[EventHandler]) -> None:
        self._handlers = handlers

    async def dispatch(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        """
        派发事件到第一个匹配的 handler。

        遍历 handler 列表，找到第一个 matches() 返回 True 的 handler，
        调用其 handle() 并 yield 所有 SSE 字符串，然后 return（不继续传递）。
        无匹配时静默丢弃。
        """
        event_type: str = event["event"]
        node_name:  str = event.get("metadata", {}).get("langgraph_node", "")
        event_name: str = event.get("name", "")

        for handler in self._handlers:
            if handler.matches(event_type, node_name, event_name):
                async for chunk in handler.handle(event, ctx):
                    yield chunk
                return


# ── 全局单例（供 stream.py 使用） ────────────────────────────────────────────
dispatcher = EventDispatcher(_HANDLERS)
