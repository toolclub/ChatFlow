"""
VisionNode：视觉理解节点

职责：
  - 图的前置节点（位于 semantic_cache_check 之后、route_model 之前）
  - 检测 state["images"] 是否为空
  - 有图片时调用本地 Ollama 视觉模型（VISION_MODEL / VISION_BASE_URL）
  - 生成图片内容的文字描述，写入 state["vision_description"]
  - 无图片时直接返回空描述，零性能开销

下游节点使用方式：
  - route_model：将 vision_description 纳入路由决策（更精准的模型选择）
  - planner：将 vision_description 作为规划输入（直接读取，无需重复调用视觉模型）

设计原则：
  - 此节点提取自原 planner 内联的 Ollama 视觉预处理逻辑
  - VISION_MODEL 未配置时静默降级，返回空描述不影响后续流程
"""
import asyncio
import logging

from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
from graph.nodes.base import BaseNode
from graph.state import GraphState

logger = logging.getLogger("graph.nodes.vision")


class VisionNode(BaseNode):
    """视觉理解节点：调用本地视觉模型分析图片内容并写入 vision_description。"""

    @property
    def name(self) -> str:
        return "vision_node"

    async def execute(self, state: GraphState) -> dict:
        """
        视觉理解执行逻辑：

          1. 无图片 → 立即返回空描述（零延迟）
          2. VISION_MODEL 未配置 → 静默降级，返回空描述
          3. 调用 Ollama 视觉模型生成描述
          4. 返回 {"vision_description": "..."}
        """
        images = state.get("images", [])

        if not images:
            return {"vision_description": ""}

        # 视觉模型未配置时降级（仍能继续后续流程，只是路由决策时无内容可用）
        if not VISION_MODEL:
            logger.warning(
                "VisionNode | VISION_MODEL 未配置，跳过视觉分析 | conv=%s",
                state.get("conv_id", ""),
            )
            return {"vision_description": ""}

        description = await self._analyze_images(
            images=images,
            user_msg=state.get("user_message", ""),
            conv_id=state.get("conv_id", ""),
        )
        return {"vision_description": description}

    async def _analyze_images(
        self,
        images: list[str],
        user_msg: str,
        conv_id: str,
    ) -> str:
        """
        调用本地 Ollama 视觉模型（VISION_BASE_URL + VISION_MODEL）分析图片。

        图片格式：data URL 或纯 base64，统一转为 data URL 传入。
        超时 60s，失败时静默降级（返回空字符串）。

        此逻辑提取自原 planner 内联代码，集中到此节点后 planner 直接读取 state 字段。
        """
        try:
            from openai import AsyncOpenAI

            # 构建多模态消息：图片 + 描述提示
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
                    "请仔细观察图片，用中文详细描述你看到的内容。"
                    "重点描述：错误信息、代码片段、界面异常、文字内容、关键数据等。"
                    "描述要具体，方便后续推理分析。"
                ),
            })

            client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
            resp   = await asyncio.wait_for(
                client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=[{"role": "user", "content": vision_content}],
                    temperature=0.1,
                ),
                timeout=60.0,
            )
            description = resp.choices[0].message.content or ""
            logger.info(
                "VisionNode 完成 | conv=%s | model=%s | desc_len=%d | preview='%.200s'",
                conv_id, VISION_MODEL, len(description), description,
            )
            return description

        except asyncio.TimeoutError:
            logger.warning(
                "VisionNode 超时（60s），降级为空描述 | conv=%s | model=%s",
                conv_id, VISION_MODEL,
            )
            return ""
        except Exception as exc:
            logger.warning(
                "VisionNode 异常，降级为空描述 | conv=%s | error=%s",
                conv_id, exc,
            )
            return ""
