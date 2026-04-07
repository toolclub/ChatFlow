"""
RouteNode：路由决策节点

职责：
  - 分析用户消息（+ 图片视觉描述），选择最合适的处理路由
  - 路由类型：chat / code / search / search_code
  - 根据路由和图片情况选择 tool_model / answer_model
  - 若存在 vision_description（由 VisionNode 生成），纳入路由决策
"""
import logging

from config import (
    ROUTER_MODEL,
    ROUTE_MODEL_MAP,
    SEARCH_MODEL,
    VISION_MODEL,
)
from graph.event_types import RouteNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.nodes.route")

from prompts import load_prompt

# 路由提示词从 prompts/nodes/route.md 加载
_ROUTE_PROMPT = load_prompt("nodes/route")

# 路由标签优先顺序（search_code 必须在 search 之前，防止部分匹配）
_ROUTE_CANDIDATES = ("search_code", "search", "code", "chat")


class RouteNode(BaseNode):
    """路由决策节点：根据用户消息和图片视觉描述，选择最合适的模型和路由。"""

    @property
    def name(self) -> str:
        return "route_model"

    async def execute(self, state: GraphState) -> RouteNodeOutput:
        """
        路由决策逻辑：
          1. 读取 vision_description（由 VisionNode 提前生成，无图片时为空字符串）
          2. 构建路由输入（文字 + 图片描述）
          3. 调用路由 LLM 决策
          4. 根据路由和图片情况选择模型
        """
        from logging_config import get_conv_logger

        user_msg      = state["user_message"]
        has_images    = bool(state.get("images"))
        vision_desc   = state.get("vision_description", "")

        llm = get_chat_llm(model=ROUTER_MODEL, temperature=0.0)

        # ── 构建路由输入 ────────────────────────────────────────────────────
        # 优先使用 vision_description（由 VisionNode 生成的图片内容文字描述）；
        # 若无描述但有图片（视觉节点降级），退回到图片数量提示。
        if has_images:
            if vision_desc:
                routing_input = (
                    f"[图片内容分析]\n{vision_desc}\n\n"
                    f"[用户请求]\n{user_msg}"
                )
            else:
                n = len(state["images"])
                routing_input = f"[用户附带了 {n} 张图片]\n用户消息：{user_msg}"
        else:
            routing_input = f"用户消息：{user_msg}"

        # ── 流式调用路由 LLM（边推送 thinking 事件，边收集结果） ──────────────
        messages = [{"role": "user", "content": f"{_ROUTE_PROMPT}\n\n{routing_input}"}]
        from logging_config import log_prompt
        log_prompt(state.get("conv_id", ""), "route_model", ROUTER_MODEL, messages)

        from langchain_core.callbacks.manager import adispatch_custom_event
        _THINK_PREFIX = "\x00THINK\x00"
        content_parts: list[str] = []
        try:
            async for delta in llm.astream(messages, temperature=0.0, timeout=30.0):
                if delta.startswith(_THINK_PREFIX):
                    thinking_text = delta[len(_THINK_PREFIX):]
                    await adispatch_custom_event(
                        "llm_thinking", {"content": thinking_text, "node": "route_model"},
                    )
                else:
                    content_parts.append(delta)
            raw = "".join(content_parts).strip().lower()
        except Exception as exc:
            logger.warning(
                "route_model 流式调用异常 | conv=%s | model=%s | error=%s，降级到 search_code",
                state.get("conv_id", ""), ROUTER_MODEL, exc,
            )
            raw = "search_code"
        if not raw:
            logger.warning(
                "route_model 返回空内容 | conv=%s | model=%s，降级到 search_code",
                state.get("conv_id", ""), ROUTER_MODEL,
            )
            raw = "search_code"

        # 解析路由标签
        route = "chat"
        for candidate in _ROUTE_CANDIDATES:
            if candidate in raw:
                route = candidate
                break

        # ── 模型选择 ────────────────────────────────────────────────────────
        # VisionNode 已在上游完成图片分析并写入 vision_description。
        # 若描述非空，说明图片已被预处理为文字，下游无需视觉能力，
        # 直接用路由决策的主模型（MiniMax 等推理模型）即可。
        # 仅当 VisionNode 降级失败（vision_desc 为空）且有原始图片时，
        # 才回退到视觉模型，保证降级安全。
        answer_model = ROUTE_MODEL_MAP.get(route, state["model"])
        needs_tools  = route in ("search", "search_code")
        tool_model   = SEARCH_MODEL if needs_tools else answer_model

        if has_images and not vision_desc:
            # VisionNode 降级：描述为空，回退视觉模型直接处理原始图片
            fallback = VISION_MODEL or ROUTE_MODEL_MAP.get("chat", state["model"])
            tool_model   = fallback
            answer_model = fallback

        get_conv_logger(state.get("client_id", ""), state.get("conv_id", "")).info(
            "路由决策 | route=%s | has_images=%s | vision_desc_len=%d "
            "| tool_model=%s | answer_model=%s | user_msg=%.60s",
            route, has_images, len(vision_desc),
            tool_model, answer_model, user_msg,
        )

        return {
            "route":        route,
            "tool_model":   tool_model,
            "answer_model": answer_model,
        }
