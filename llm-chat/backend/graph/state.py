"""
LangGraph Agent 状态定义
"""
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class PlanStep(TypedDict):
    """执行计划中的单个步骤"""
    id: str
    title: str
    description: str
    status: str         # 'pending' | 'running' | 'done' | 'failed'
    result: str


class GraphState(TypedDict):
    # ── 输入 ────────────────────────────────────────────────────────────────
    conv_id: str
    client_id: str                             # 浏览器唯一标识（用于日志分文件）
    user_message: str
    model: str
    temperature: float

    # ── 消息列表 ────────────────────────────────────────────────────────────
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # ── 记忆上下文 ──────────────────────────────────────────────────────────
    long_term_memories: list[str]
    forget_mode: bool

    # ── 输出 ────────────────────────────────────────────────────────────────
    full_response: str
    compressed: bool
    tool_model: str
    answer_model: str
    route: str

    # ── 认知规划 ────────────────────────────────────────────────────────────
    plan: list[PlanStep]
    current_step_index: int
    reflection: str
    reflector_decision: str
    step_iterations: int
