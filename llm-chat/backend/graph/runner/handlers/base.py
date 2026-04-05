"""
EventHandler：事件处理器抽象基类

职责：监听特定的 LangGraph astream_events 事件类型，将其翻译为 SSE 字符串。

设计模式：职责链 + 策略模式
  - matches() 决定是否处理该事件（职责链）
  - handle()  决定如何翻译事件为 SSE（策略）
  - EventDispatcher 持有所有 handler，顺序匹配后派发

扩展方式：
  1. 在 handlers/ 目录新建文件，实现 EventHandler 子类
  2. 在 dispatcher.py 的 _HANDLERS 列表中注册（注意顺序）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator

from graph.runner.context import StreamContext


class EventHandler(ABC):
    """
    事件处理器抽象基类。

    每个子类负责处理一类 LangGraph 事件，互相独立、职责单一。
    matches + handle 共同构成一个处理策略：先判断是否匹配，再执行处理。
    """

    @abstractmethod
    def matches(self, event_type: str, node_name: str, event_name: str) -> bool:
        """
        判断是否处理该事件。

        参数：
            event_type: LangGraph 事件类型（on_chain_start / on_chain_end 等）
            node_name:  事件所属的节点名称（来自 metadata.langgraph_node）
            event_name: 事件名称（来自 event["name"]）
        """
        ...

    @abstractmethod
    async def handle(
        self, event: dict, ctx: StreamContext
    ) -> AsyncGenerator[str, None]:
        """
        处理事件并 yield SSE 字符串。

        参数：
            event: 原始 LangGraph 事件 dict
            ctx:   当前会话流式上下文（可读写）

        Yields:
            SSE 格式字符串（"data: {...}\\n\\n"）
        """
        ...
