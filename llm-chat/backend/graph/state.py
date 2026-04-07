"""
LangGraph Agent 状态定义

GraphState 是图中所有节点共享的状态容器，由 LangGraph 负责在节点间传递和合并。
messages 字段使用 add_messages reducer，支持跨节点追加消息。
"""
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class PlanStep(TypedDict):
    """执行计划中的单个步骤。"""
    id: str
    title: str
    description: str
    status: str   # 'pending' | 'running' | 'done' | 'failed'
    result: str


class GraphState(TypedDict):
    # ── 输入 ────────────────────────────────────────────────────────────────
    conv_id: str
    client_id: str                              # 浏览器唯一标识（用于日志分文件）
    user_message: str
    images: list[str]                           # 图片列表（完整 data URL 或纯 base64）
    model: str
    temperature: float

    # ── 消息列表（LangGraph add_messages reducer 自动追加） ─────────────────
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

    # ── 语义缓存 ────────────────────────────────────────────────────────────
    cache_hit: bool                             # True 表示本轮命中缓存，跳过 LLM
    cache_similarity: float                     # 缓存命中时的语义相似度分数（未命中为 0.0）

    # ── 视觉理解（由 VisionNode 写入，route_model / planner 读取） ───────────
    vision_description: str                     # Ollama 视觉模型对图片的文字描述

    # ── 认知规划 ────────────────────────────────────────────────────────────
    force_plan: list[PlanStep]       # 用户编辑后的强制计划（非空时 planner 跳过 LLM 直接使用）
    plan: list[PlanStep]
    plan_id: str                 # plan_steps 表主键（planner 写入，后续节点只读）
    plan_goal: str               # 原始任务目标（续写时 user_message 是"继续"，需单独保存 goal）
    current_step_index: int
    reflection: str
    reflector_decision: str
    step_iterations: int
    step_results: list[str]      # 每步 full_response 的累积列表（reflector 写入，save_response 读取）

    # ── 澄清问询（模型无法直接回答时，向用户确认意图） ───────────────────────
    needs_clarification: bool   # True 表示本轮需要用户澄清，跳过 DB 保存
