"""
RetrieveContextNode：上下文检索节点

职责：
  - 从 Qdrant 检索长期记忆（若启用）
  - 用余弦相似度判断是否触发「遗忘模式」（与最近话题无关时跳过历史）
  - 调用 context_builder 组装历史消息 + 系统提示
  - 构建用户消息（有图片时使用多模态格式）

工厂注入：
  - tool_names: 用于 context_builder 中标注工具使用情况
"""
import logging

from langchain_core.messages import HumanMessage

from config import LONGTERM_MEMORY_ENABLED
from db.artifact_store import get_artifacts_by_ids
from graph.nodes.base import BaseNode
from graph.state import GraphState
from memory import store as memory_store
from memory.context_builder import build_messages


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


async def _build_file_preamble(conv_id: str, file_ids: list[int]) -> str:
    """根据已绑定的 artifacts 生成文件清单前缀（注入到 HumanMessage）。"""
    if not file_ids:
        return ""
    rows = await get_artifacts_by_ids(file_ids, conv_id)
    if not rows:
        return ""
    lines = [f"[用户已上传 {len(rows)} 个文件到沙箱，可用 run_shell / 文件工具直接读取]"]
    for r in rows:
        lines.append(f"- {r['name']} ({_fmt_size(r.get('size') or 0)})  路径: {r['path']}")
    return "\n".join(lines)

logger = logging.getLogger("graph.nodes.retrieve_context")


class RetrieveContextNode(BaseNode):
    """
    上下文检索节点。

    通过 __init__ 注入 tool_names，避免全局依赖。
    """

    def __init__(self, tool_names: list[str]) -> None:
        """
        参数：
            tool_names: 当前可用工具名称列表，用于 context_builder 构建工具提示
        """
        self._tool_names = tool_names

    @property
    def name(self) -> str:
        return "retrieve_context"

    async def execute(self, state: GraphState) -> dict:
        """
        执行上下文检索：
          1. 从 Qdrant 检索长期记忆（若启用）
          2. 判断是否触发遗忘模式
          3. 组装历史消息（含 system prompt）
          4. 构建用户消息（纯文本 or 多模态）
          5. 初始化认知规划字段
        """
        conv_id  = state["conv_id"]
        user_msg = state["user_message"]
        conv     = memory_store.get(conv_id)

        long_term: list[str] = []
        forget_mode = False

        # ── 长期记忆检索（embedding 失败时降级为无记忆，不阻断主流程） ────────
        if LONGTERM_MEMORY_ENABLED and user_msg:
            try:
                from rag import retriever as rag_retriever
                long_term = await rag_retriever.search_memories(conv_id, user_msg)

                # 长期记忆为空时，判断是否与当前话题相关（触发遗忘模式）
                if not long_term and conv:
                    if conv.mid_term_summary:
                        relevant = await rag_retriever.is_relevant_to_summary(
                            user_msg, conv.mid_term_summary
                        )
                    else:
                        recent = [m.content for m in conv.messages if m.role == "user"][-2:]
                        if recent:
                            relevant = await rag_retriever.is_relevant_to_recent(user_msg, recent)
                        else:
                            relevant = True
                    forget_mode = not relevant
            except Exception as exc:
                logger.warning("长期记忆检索失败（已降级为无记忆）| conv=%s | error=%s", conv_id, exc)

        # ── 组装历史消息 ────────────────────────────────────────────────────
        history_messages = build_messages(conv, long_term, forget_mode, self._tool_names)

        # ── 构建用户消息 ────────────────────────────────────────────────────
        # 优先使用 VisionNode 产出的文字描述（vision_description）。
        # 图片已由 VisionNode 预处理：主推理模型只需接收文字即可，
        # 无需视觉能力，也不会因携带大 base64 导致超时/断连。
        #
        # 降级逻辑：vision_description 为空（VisionNode 失败）且有原始图片时，
        # 回退到多模态消息格式，由路由决策的视觉模型直接处理。
        images            = state.get("images", [])
        vision_description = state.get("vision_description", "")
        file_ids          = state.get("file_ids", []) or []

        # 上传文件清单前缀（由 StreamSession 绑定后写入 state）
        file_preamble = await _build_file_preamble(conv_id, file_ids)
        if file_preamble:
            logger.info(
                "retrieve_context 注入文件清单 | conv=%s | count=%d",
                conv_id, len(file_ids),
            )

        def _prepend_preamble(text: str) -> str:
            """把文件清单前缀拼到文本最前面。"""
            if not file_preamble:
                return text
            return f"{file_preamble}\n\n{text}" if text else file_preamble

        if vision_description:
            # 主路径：将图片描述注入为文字上下文，主模型做推理
            if user_msg:
                combined = f"[图片内容]\n{vision_description}\n\n{user_msg}"
            else:
                combined = vision_description
            history_messages.append(HumanMessage(content=_prepend_preamble(combined)))
            logger.info(
                "retrieve_context 注入图片描述 | conv=%s | desc_len=%d",
                conv_id, len(vision_description),
            )
        elif images:
            # 降级路径：VisionNode 未能生成描述，传递原始图片（需视觉模型处理）
            multimodal_content: list = []
            for img in images:
                url = img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                logger.info(
                    "retrieve_context 降级图片URL | conv=%s | prefix=%.40s | len=%d",
                    conv_id, url[:40], len(url),
                )
                multimodal_content.append({"type": "image_url", "image_url": {"url": url}})
            text_part = _prepend_preamble(user_msg)
            if text_part:
                multimodal_content.append({"type": "text", "text": text_part})
            history_messages.append(HumanMessage(content=multimodal_content))
        else:
            history_messages.append(HumanMessage(content=_prepend_preamble(user_msg)))

        return {
            "messages":          history_messages,
            "long_term_memories": long_term,
            "forget_mode":       forget_mode,
            # 初始化认知规划字段（每轮重置）
            "plan":               [],
            "plan_id":            "",
            "current_step_index": 0,
            "step_iterations":    0,
            "reflector_decision": "",
            "reflection":         "",
            "step_results":       [],
        }
