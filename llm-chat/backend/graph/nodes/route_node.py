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

# ── 路由提示词 ────────────────────────────────────────────────────────────────
_ROUTE_PROMPT = """你是一个智能路由器。分析用户消息（和附带图片说明），选择最合适的处理方式，输出以下标签之一：

- chat         直接回答，无需联网或工具：
                 日常对话、解释概念、翻译、写作、数学、逻辑推理、分析图片内容

- code         纯代码任务，需求明确、无需查资料：
                 编写/调试/重构/解释代码，依据图片内容直接生成代码

- search       需联网查询，不涉及写代码：
                 实时/最新信息（新闻、股价、天气、版本号）、具体事实核查、
                 查询图片中出现的商品/地点/人物/文字的详细信息

- search_code  需先查资料再写代码：
                 查官方文档/API 后写代码、根据图片内容查资料再实现功能、
                 用户要实现/完成/开发某个产品或系统（但你不清楚它具体是什么）、
                 模仿/参考某个产品做开发（需先了解该产品）

【判断原则】
- 图片纯分析（描述/解读/OCR/情感）→ chat
- 图片内容需要联网核实或延伸查询 → search
- 根据图片直接写代码且需求明确 → code
- 根据图片写代码但需先查资料 → search_code
- 明确要求查官方/文档后写代码 → search_code
- 只写代码需求明确不查资料 → code
- 只查信息不写代码 → search
- 要实现/完成/开发某产品，但该产品你不熟悉或名称模糊 → search_code（先搜索了解再实现）
- 不确定 search 还是 search_code → 优先 search_code

只输出标签本身，例如：chat"""

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

        # ── 调用路由 LLM ────────────────────────────────────────────────────
        messages = [{"role": "user", "content": f"{_ROUTE_PROMPT}\n\n{routing_input}"}]
        from logging_config import log_prompt
        log_prompt(state.get("conv_id", ""), "route_model", ROUTER_MODEL, messages)
        completion = await llm.ainvoke(messages, timeout=30.0)
        raw = (completion.choices[0].message.content or "").strip().lower()

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
