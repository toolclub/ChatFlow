"""
LangGraph Agent 状态定义

设计说明：
  - messages：当前轮次的消息（含工具调用/结果），使用 add_messages reducer 自动追加
  - 对话完整历史存储在 memory/store.py（ConversationStore），不在 GraphState 中
  - 每次请求都是一个独立的图执行，不依赖 LangGraph checkpointer 跨轮持久化
"""
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class GraphState(TypedDict):
    # ── 输入（每次请求开始时设定） ──────────────────────────────────────────
    conv_id: str                              # 对话 ID
    user_message: str                         # 当前用户消息
    model: str                                # 使用的模型名称
    temperature: float                        # 采样温度

    # ── 消息列表（add_messages 自动追加，支持多步工具调用） ─────────────────
    # 包含：滑动窗口历史 + 当前轮 HumanMessage + AIMessage(tool_calls) + ToolMessage
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # ── 记忆上下文（由 retrieve_context 节点填充） ─────────────────────────
    long_term_memories: list[str]             # Qdrant 检索到的相关历史
    forget_mode: bool                         # True = 话题切换，只保留近期上下文

    # ── 输出（由 call_model / compress_memory 节点填充） ──────────────────
    full_response: str                        # LLM 最终回复内容（累积自最后一次 call_model）
    compressed: bool                          # 是否触发了本轮压缩
    tool_model: str         # 专门用于工具调用
    answer_model: str       # 专门用于最终回答
    route: str              # 路由结果: 'chat' | 'code' | 'search' | ''