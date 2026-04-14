"""
节点输出结构 + astream_events 事件包装，两者同源。

━━━ 同源关系 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  nodes.py 里每个节点 return 的 dict
      ↓ LangGraph 把它放进 event["data"]["output"]
  runner.py 的 EventHandler 从 event["data"]["output"] 读出来

  所以节点 return 的结构 和 EventHandler 读到的结构是同一个东西。
  这里用 TypedDict 统一定义节点输出，nodes.py 用作 return 类型注解，
  runner.py 的 from_event() 用同一个类型解析，不再各自裸写 dict.get()。

━━━ 分两层 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  层 1：节点输出 TypedDict（XxxNodeOutput）
        nodes.py return 时用，同时也是 event["data"]["output"] 的类型

  层 2：事件包装 dataclass（XxxEvent）
        runner.py 的 handler 通过 from_event() 拿到，有 IDE 自动补全

━━━ 对应关系 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  CacheHitNodeOutput / CacheHitEndEvent    ← on_chain_end   + semantic_cache_check
  RouteNodeOutput   / RouteEndEvent    ← on_chain_end   + route_model
  PlannerNodeOutput / PlannerEndEvent  ← on_chain_end   + planner
  ReflectorNodeOutput / ReflectorEndEvent ← on_chain_end + reflector
  CompressNodeOutput / CompressEndEvent   ← on_chain_end + compress_memory
  LLMStreamEvent                       ← on_chat_model_stream + call_model*
  ToolStartEvent                       ← on_tool_start
  ToolEndEvent                         ← on_tool_end
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence, TypedDict

from graph.state import PlanStep  # 唯一定义，两边共用


# ══════════════════════════════════════════════════════════════════════════════
# 层 1：节点输出 TypedDict
# nodes.py 的节点函数 return 类型注解用这些，同时也是 event data.output 的类型
# ══════════════════════════════════════════════════════════════════════════════

class CacheHitNodeOutput(TypedDict, total=False):
    """semantic_cache_check 节点 return 的结构"""
    cache_hit: bool       # True 表示命中缓存
    full_response: str    # 命中时：缓存的答案；未命中时：空字符串
    cache_similarity: float  # 命中时的相似度分数


class RouteNodeOutput(TypedDict, total=False):
    """route_model 节点 return 的结构"""
    route: str         # chat | code | search | search_code
    tool_model: str    # 工具调用阶段使用的模型
    answer_model: str  # 最终回复使用的模型


class PlannerNodeOutput(TypedDict, total=False):
    """planner 节点 return 的结构"""
    plan: list[PlanStep]
    plan_id: str
    plan_goal: str
    current_step_index: int
    step_iterations: int
    step_results: list[str]


class ReflectorNodeOutput(TypedDict, total=False):
    """reflector 节点 return 的结构"""
    plan: list[PlanStep]
    reflection: str
    reflector_decision: str   # done | continue | retry
    messages: list            # continue 时注入的下一步 HumanMessage
    current_step_index: int
    step_iterations: int
    step_results: list[str]
    full_response: str        # 兜底 response（工具结果摘要），确保 save_response 不存空
    forget_mode: bool


class CompressNodeOutput(TypedDict, total=False):
    """compress_memory 节点 return 的结构"""
    compressed: bool


# ══════════════════════════════════════════════════════════════════════════════
# 层 2：事件包装 dataclass
# runner.py 的 EventHandler 通过 from_event() 拿到，字段有 IDE 提示
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CacheHitEndEvent:
    """semantic_cache_check 节点结束事件"""
    cache_hit: bool
    full_response: str
    cache_similarity: float

    @classmethod
    def from_event(cls, event: dict) -> CacheHitEndEvent:
        output: CacheHitNodeOutput = event["data"].get("output", {})
        if not isinstance(output, dict):
            return cls(cache_hit=False, full_response="", cache_similarity=0.0)
        return cls(
            cache_hit=output.get("cache_hit", False),
            full_response=output.get("full_response", ""),
            cache_similarity=output.get("cache_similarity", 0.0),
        )


@dataclass
class RouteEndEvent:
    """route_model 节点结束事件"""
    route: str
    tool_model: str
    answer_model: str

    @classmethod
    def from_event(cls, event: dict) -> RouteEndEvent | None:
        output: RouteNodeOutput = event["data"].get("output", {})
        if not isinstance(output, dict):
            return None
        return cls(
            route=output.get("route", ""),
            tool_model=output.get("tool_model", ""),
            answer_model=output.get("answer_model", ""),
        )


@dataclass
class PlannerEndEvent:
    """planner 节点结束事件"""
    plan: list[PlanStep]
    current_step_index: int
    step_iterations: int

    @classmethod
    def from_event(cls, event: dict) -> PlannerEndEvent | None:
        output: PlannerNodeOutput = event["data"].get("output", {})
        if not isinstance(output, dict):
            return None
        return cls(
            plan=output.get("plan", []),
            current_step_index=output.get("current_step_index", 0),
            step_iterations=output.get("step_iterations", 0),
        )


@dataclass
class ReflectorEndEvent:
    """reflector 节点结束事件"""
    plan: list[PlanStep]
    reflection: str
    reflector_decision: str   # done | continue | retry

    @classmethod
    def from_event(cls, event: dict) -> ReflectorEndEvent | None:
        output: ReflectorNodeOutput = event["data"].get("output", {})
        if not isinstance(output, dict):
            return None
        return cls(
            plan=output.get("plan", []),
            reflection=output.get("reflection", ""),
            reflector_decision=output.get("reflector_decision", ""),
        )


@dataclass
class CompressEndEvent:
    """compress_memory 节点结束事件"""
    compressed: bool

    @classmethod
    def from_event(cls, event: dict) -> CompressEndEvent:
        output: CompressNodeOutput = event["data"].get("output", {})
        compressed = output.get("compressed", False) if isinstance(output, dict) else False
        return cls(compressed=compressed)


@dataclass
class LLMStreamEvent:
    """LLM 流式 token 事件（call_model / call_model_after_tool）"""
    content: str   # 原始 token，未过滤 think 块
    node: str      # call_model | call_model_after_tool

    @classmethod
    def from_event(cls, event: dict) -> LLMStreamEvent | None:
        chunk = event["data"].get("chunk")
        if not chunk or not chunk.content:
            return None
        return cls(
            content=chunk.content,
            node=event.get("metadata", {}).get("langgraph_node", ""),
        )


@dataclass
class ToolStartEvent:
    """工具调用开始事件"""
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, event: dict) -> ToolStartEvent:
        return cls(
            name=event.get("name", ""),
            input=event["data"].get("input", {}),
        )


@dataclass
class ToolEndEvent:
    """工具调用结束事件，raw_output 已从 ToolMessage 提取为字符串"""
    name: str
    raw_output: str

    @classmethod
    def from_event(cls, event: dict) -> ToolEndEvent:
        name = event.get("name", "")
        output = event["data"].get("output", "")
        if hasattr(output, "content"):
            raw = output.content if isinstance(output.content, str) else str(output.content)
        else:
            raw = str(output)
        return cls(name=name, raw_output=raw)
