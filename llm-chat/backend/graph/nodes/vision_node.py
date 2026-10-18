"""
VisionNode：视觉理解节点

职责：
  - 图的前置节点（位于 semantic_cache_check 之后、route_model 之前）
  - 检测 state["images"] 是否为空
  - 有图片时：流式调用视觉模型（VISION_MODEL / VISION_BASE_URL）
      - 每个 token 通过 vision_token 事件推送给前端（显示在 thinking 块）
      - 最终描述写入 state["vision_description"]
  - 无图片时直接返回空描述，零性能开销

下游节点使用方式：
  - route_model：将 vision_description 纳入路由决策（更精准的模型选择）
  - retrieve_context：将 vision_description 注入为文字上下文，主推理模型无需视觉能力
  - call_model：vision_description 非空时走主 LLM 路径（MiniMax 等），
                不再由视觉模型负责推理，避免 GLM-4.6V 等视觉模型因无推理能力而断连

设计原则：
  - "分析归分析，推理归推理"：视觉模型只做描述，推理交给主模型
  - 流式输出：前端实时看到图像分析过程，用户体验与 LLM 思考一致
  - VISION_MODEL 未配置时静默降级，返回空描述不影响后续流程
"""
import asyncio
import logging

from langchain_core.callbacks.manager import adispatch_custom_event

from config import VISION_MODEL
from graph.nodes.base import BaseNode, track_usage
from graph.state import GraphState
from llm.chat import get_vision_async_client
from services import llm_provider_service

logger = logging.getLogger("graph.nodes.vision")

# 视觉分析超时（秒）：GLM-4.6V 流式模式
_VISION_TIMEOUT = 60.0


class VisionNode(BaseNode):
    """视觉理解节点：流式调用视觉模型分析图片，token 级别推送给前端。"""

    @property
    def name(self) -> str:
        return "vision_node"

    @track_usage
    async def execute(self, state: GraphState) -> dict:
        """
        视觉理解执行逻辑：

          1. 无图片 → 立即返回空描述（零延迟）
          2. VISION_MODEL 未配置 → 静默降级，返回空描述
          3. 通知前端进入"图像解析中"状态
          4. 流式调用视觉模型，逐 token 派发 vision_token 事件
          5. 返回 {"vision_description": "...", "usage": usage_data}
        """
        images = state.get("images", [])

        if not images:
            return {"vision_description": ""}

        # 视觉模型未配置时降级（仍能继续后续流程）：
        # env 模式看 VISION_MODEL；管理页激活的供应商配置了 vision_model 时视为已配置。
        # 注意不能用 llm_provider_service.resolve_vision() 判空 —— 它的回退链兜到 CHAT_MODEL，永远非空。
        snapshot = llm_provider_service.get_snapshot()
        if not (VISION_MODEL or (snapshot and snapshot.vision_model)):
            logger.warning(
                "VisionNode | VISION_MODEL 未配置，跳过视觉分析 | conv=%s",
                state.get("conv_id", ""),
            )
            return {"vision_description": ""}

        # 通知前端进入"图像解析中"状态（触发状态标签切换）
        await adispatch_custom_event("vision_analyze", {"image_count": len(images)})

        description, usage = await self._analyze_images(
            images=images,
            user_msg=state.get("user_message", ""),
            conv_id=state.get("conv_id", ""),
        )
        return {"vision_description": description, "usage": usage}

    async def _analyze_images(
        self,
        images: list[str],
        user_msg: str,
        conv_id: str,
    ) -> tuple[str, dict]:
        """
        流式调用视觉模型分析图片，逐 token 通过 vision_token 事件推给前端。

        前端 VisionStreamHandler 将 vision_token 转换为 {"thinking": delta} SSE，
        显示在消息的"思考过程"折叠块中，让用户看到图像分析的实时过程。

        超时 60s，失败时静默降级（返回空字符串），由 retrieve_context 的降级路径处理。
        """
        try:
            vision_content: list = []
            for img in images:
                url = img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                vision_content.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })
            vision_content.append({
                "type": "text",
                "text": (
                    "请用中文简要描述图片内容（150字以内）："
                    "主题/核心元素、文字内容、色彩风格、用途或场景。"
                    "直接描述事实，不要分析或评价。"
                ),
            })

            # 统一走工厂：base/key/model 均来自 llm_provider_service.resolve_vision()
            # （管理页供应商配置了 vision_model 时随供应商切换，否则 .env VISION_*）
            vision_model = llm_provider_service.resolve_vision()[2]
            client = get_vision_async_client()

            description, usage = await asyncio.wait_for(
                self._stream_vision(client, vision_model, vision_content, conv_id),
                timeout=_VISION_TIMEOUT,
            )
            return description, usage

        except asyncio.TimeoutError:
            logger.warning(
                "VisionNode 流式超时（%.0fs），降级为空描述 | conv=%s | model=%s",
                _VISION_TIMEOUT, conv_id, llm_provider_service.resolve_vision()[2],
            )
            return "", {}
        except Exception as exc:
            logger.warning(
                "VisionNode 流式异常，降级为空描述 | conv=%s | error=%s",
                conv_id, exc,
            )
            return "", {}

    @staticmethod
    async def _stream_vision(client, vision_model: str, vision_content: list, conv_id: str) -> tuple[str, dict]:
        """
        流式调用视觉模型，逐 token 通过统一 emit_thinking 推给前端。
        视觉输出作为 node=vision / phase=content 的 segment，
        落在消息级思考区（step_index=None），与主模型思考段自然隔离。
        """
        parts: list[str] = []
        usage_data = {}

        vision_messages = [{"role": "user", "content": vision_content}]
        from logging_config import log_prompt
        log_prompt(conv_id, "vision_node", vision_model, vision_messages)

        stream = await client.chat.completions.create(
            model=vision_model,
            messages=[{"role": "user", "content": vision_content}],
            temperature=0.1,
            stream=True,
            stream_options={"include_usage": True},
        )

        try:
            async for chunk in stream:
                if hasattr(chunk, "usage") and chunk.usage:
                    u = chunk.usage
                    usage_data = {
                        "prompt_tokens": u.prompt_tokens,
                        "completion_tokens": u.completion_tokens,
                        "total_tokens": u.total_tokens,
                    }
                    continue

                delta = ""
                if chunk.choices:
                    delta = chunk.choices[0].delta.content or ""
                if delta:
                    parts.append(delta)
                    await BaseNode.emit_thinking("vision", "content", delta, None)
        except Exception as exc:
            # 视觉分析中断时保留已生成的部分描述（不丢弃已有信息）
            partial = "".join(parts)
            if partial:
                logger.warning(
                    "VisionNode 流式中断，保留部分描述 | conv=%s | partial_len=%d | error=%s",
                    conv_id, len(partial), exc,
                )
                await BaseNode.emit_thinking(
                    "vision", "content", "\n[图像分析中断，以上为部分结果]", None,
                )
                return partial, usage_data
            logger.error("VisionNode 流式失败且无部分结果 | conv=%s | %s", conv_id, exc)
            return "", {}

        description = "".join(parts)
        logger.info(
            "VisionNode 流式完成 | conv=%s | model=%s | desc_len=%d | preview='%.200s'",
            conv_id, vision_model, len(description), description,
        )
        return description, usage_data
