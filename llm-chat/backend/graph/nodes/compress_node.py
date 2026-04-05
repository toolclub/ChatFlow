"""
CompressNode：记忆压缩节点

职责：
  - 按需触发对话压缩（超过阈值时生成滚动摘要 + 写入 Qdrant）
  - 在 save_response 之后运行，不影响流式输出
  - 本节点只负责调用触发，具体压缩逻辑封装在 memory.compressor 中
"""
import logging

from graph.event_types import CompressNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState
from memory.compressor import maybe_compress

logger = logging.getLogger("graph.nodes.compress")


class CompressNode(BaseNode):
    """记忆压缩节点：触发对话上下文的滚动摘要压缩。"""

    @property
    def name(self) -> str:
        return "compress_memory"

    async def execute(self, state: GraphState) -> CompressNodeOutput:
        """
        触发压缩并返回压缩状态。

        maybe_compress 内部会检查是否满足压缩阈值，未达阈值时为空操作。
        """
        conv_id   = state["conv_id"]
        client_id = state.get("client_id", "")
        try:
            compressed = await maybe_compress(conv_id)
            if compressed:
                from logging_config import get_conv_logger
                get_conv_logger(client_id, conv_id).info(
                    "记忆压缩触发 | conv=%s", conv_id
                )
        except Exception as exc:
            logger.error("压缩失败 | conv=%s | error=%s", conv_id, exc)
            compressed = False

        return {"compressed": compressed}
