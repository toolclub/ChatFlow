"""
SemanticCacheNode：语义缓存检查节点

职责：
  - 图的最前置节点，在任何 LLM 调用前检查语义缓存
  - 命中：写入 full_response + cache_hit=True，后续 cache_routing 跳到 save_response
  - 未命中：cache_hit=False，继续正常流程

缓存命名空间策略（由 SEMANTIC_CACHE_NAMESPACE_MODE 配置）：
  "user"   → 每个用户（client_id）独立
  "conv"   → 每个对话独立（最细粒度）
  "prompt" → 同 system prompt 跨用户共享
  "global" → 全局共享
"""
import hashlib
import logging

from config import (
    DEFAULT_SYSTEM_PROMPT,
    SEMANTIC_CACHE_NAMESPACE_MODE,
)
from graph.event_types import CacheHitNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState

logger = logging.getLogger("graph.nodes.cache")


class SemanticCacheNode(BaseNode):
    """语义缓存检查节点：图的第一个执行节点。"""

    @property
    def name(self) -> str:
        return "semantic_cache_check"

    async def execute(self, state: GraphState) -> CacheHitNodeOutput:
        """
        检查当前请求是否命中语义缓存。

        含图片的请求始终跳过缓存（图片内容不参与语义匹配）。
        """
        from cache.factory import get_cache
        from logging_config import get_conv_logger
        from memory import store as memory_store

        user_msg = state["user_message"]
        conv_id = state["conv_id"]
        client_id = state.get("client_id", "")
        clog = get_conv_logger(client_id, conv_id)

        # 强制计划时跳过缓存（用户编辑了执行计划，必须重新执行）
        if state.get("force_plan"):
            clog.info("Cache SKIP  | force_plan 模式，跳过语义缓存")
            return {"cache_hit": False, "full_response": "", "cache_similarity": 0.0}

        # 含图片时跳过缓存
        if state.get("images"):
            clog.info(
                "Cache SKIP  | 含图片请求，跳过语义缓存 | user_msg='%.60s'",
                user_msg,
            )
            return {"cache_hit": False, "full_response": "", "cache_similarity": 0.0}

        conv = memory_store.get(conv_id)
        namespace = self._derive_cache_namespace(conv, SEMANTIC_CACHE_NAMESPACE_MODE, client_id)
        cache = get_cache()
        result = await cache.lookup(user_msg, namespace)

        if result is None:
            clog.info(
                "Cache MISS  | ns=%s | 未命中，继续正常流程 | user_msg='%.60s'",
                namespace, user_msg,
            )
            return {"cache_hit": False, "full_response": "", "cache_similarity": 0.0}

        clog.info(
            "Cache HIT   | similarity=%.4f | ns=%s | matched='%.60s' | user_msg='%.60s'",
            result.similarity, namespace, result.matched_question, user_msg,
        )
        return {
            "cache_hit": True,
            "full_response": result.answer,
            "cache_similarity": result.similarity,
        }

    @staticmethod
    def _derive_cache_namespace(conv: object, mode: str, client_id: str = "") -> str:
        """
        根据命名空间模式派生缓存 namespace 字符串。

        "user"   → client_id，每个用户（浏览器）独立，多人系统推荐
        "conv"   → conv_id，每个对话独立（最细粒度，无跨会话复用）
        "global" → "global"，所有用户完全共享
        "prompt" → md5(system_prompt)[:8]，同 prompt 跨用户共享（默认）
        """
        if mode == "user":
            return f"u:{client_id}" if client_id else "u:anon"
        if mode == "conv":
            return getattr(conv, "id", "global") if conv else "global"
        if mode == "global":
            return "global"
        # 默认 "prompt" 模式
        prompt = (getattr(conv, "system_prompt", "") if conv else "") or DEFAULT_SYSTEM_PROMPT
        return hashlib.md5(prompt.encode()).hexdigest()[:8]
