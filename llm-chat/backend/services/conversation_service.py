"""
会话服务层 — 封装对话管理的业务逻辑

职责：
  - 对话 CRUD（create / get / update / delete / list）
  - 批量删除（含全链路数据清理）
  - 完整状态查询（full-state）
  - 流式状态检查

设计原则：
  - Service 层只做业务编排，不直接操作 DB（委托给 store 层）
  - 所有方法为 async，符合 spec 铁律 #5
"""
import asyncio
import logging
import uuid

from sqlalchemy import delete as sa_delete

from memory import store as memory_store
from memory.tool_events import get_tool_events
from db.database import AsyncSessionLocal
from db.models import (
    MessageModel, ArtifactModel, PlanStepModel,
    ToolEventModel, MessageDetailModel,
)
from db.artifact_store import get_artifact_meta_list
from db.plan_store import get_latest_plan_for_conv
from db.tool_store import get_tool_executions_for_conv
from db.event_store import get_latest_event_id
from config import LONGTERM_MEMORY_ENABLED, DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("services.conversation")


class ConversationService:
    """对话管理服务"""

    # ── 列表 / 单查 ──────────────────────────────────────────────────────────

    async def list_conversations(self, client_id: str = "") -> list[dict]:
        return await memory_store.db_list_conversations(client_id)

    async def get_conversation(self, conv_id: str) -> dict | None:
        return await memory_store.db_get_conversation(conv_id)

    # ── 创建 ─────────────────────────────────────────────────────────────────

    async def create_conversation(
        self, title: str = "新对话", system_prompt: str = "", client_id: str = "",
    ) -> dict:
        conv_id = str(uuid.uuid4())[:8]
        conv = await memory_store.create(
            conv_id=conv_id,
            title=title or "新对话",
            system_prompt=system_prompt or "",
            client_id=client_id,
        )
        return {"id": conv.id, "title": conv.title}

    # ── 更新 ─────────────────────────────────────────────────────────────────

    async def update_conversation(
        self, conv_id: str, title: str | None = None, system_prompt: str | None = None,
    ) -> dict:
        conv_data = await memory_store.db_get_conversation(conv_id)
        if not conv_data:
            return {"error": "对话不存在"}
        conv = memory_store.get(conv_id)
        if not conv:
            return {"error": "对话不存在"}
        if title is not None:
            conv.title = title
        if system_prompt is not None:
            conv.system_prompt = system_prompt
        await memory_store.save(conv)
        return {"ok": True}

    # ── 单个删除（完整清理） ─────────────────────────────────────────────────

    async def delete_conversation(self, conv_id: str) -> dict:
        await self._cleanup_conversation(conv_id)
        return {"ok": True}

    # ── 批量删除（完整清理） ─────────────────────────────────────────────────

    async def batch_delete_conversations(self, conv_ids: list[str]) -> dict:
        if not conv_ids:
            return {"ok": True, "deleted": 0}

        # 并行停止所有活跃流 + 清理
        tasks = [self._cleanup_conversation(cid) for cid in conv_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        deleted = sum(1 for r in results if not isinstance(r, Exception))
        errors = [
            {"conv_id": conv_ids[i], "error": str(r)}
            for i, r in enumerate(results) if isinstance(r, Exception)
        ]
        if errors:
            logger.warning("批量删除部分失败: %s", errors)

        return {"ok": True, "deleted": deleted, "errors": errors}

    # ── 完整状态查询 ──────────────────────────────────────────────────────────

    async def get_full_state(self, conv_id: str) -> dict:
        conv_data = await memory_store.db_get_conversation(conv_id)
        if not conv_data:
            return {"error": "对话不存在"}

        tool_execs, latest_plan, artifacts, last_event_id = await asyncio.gather(
            get_tool_executions_for_conv(conv_id),
            get_latest_plan_for_conv(conv_id),
            get_artifact_meta_list(conv_id),
            get_latest_event_id(conv_id),
        )

        has_streaming = any(
            not m.get("stream_completed", True)
            for m in conv_data["messages"]
            if m.get("role") == "assistant"
        )

        tool_by_msg: dict[str, list] = {}
        for t in tool_execs:
            tool_by_msg.setdefault(t["message_id"], []).append(t)

        artifact_by_msg: dict[str, list] = {}
        for a in artifacts:
            if a.get("message_id"):
                artifact_by_msg.setdefault(a["message_id"], []).append(a)

        enriched_messages = []
        for m in conv_data["messages"]:
            msg = {**m}
            msg_id = m.get("message_id", "")
            if msg_id and msg_id in tool_by_msg:
                msg["tool_executions"] = tool_by_msg[msg_id]
            if msg_id and msg_id in artifact_by_msg:
                msg["artifacts"] = artifact_by_msg[msg_id]
            enriched_messages.append(msg)

        orphan_artifacts = [a for a in artifacts if not a.get("message_id")]

        return {
            "id": conv_data["id"],
            "title": conv_data["title"],
            "status": conv_data.get("status", "active"),
            "messages": enriched_messages,
            "plan": latest_plan,
            "artifacts": orphan_artifacts,
            "has_streaming": has_streaming,
            "last_event_id": last_event_id,
        }

    # ── 流式状态 ──────────────────────────────────────────────────────────────

    async def get_streaming_status(self, conv_id: str) -> dict:
        conv_data = await memory_store.db_get_conversation(conv_id)
        if not conv_data:
            return {"streaming": False, "last_event_id": 0}
        is_streaming = conv_data.get("status") == "streaming"
        last_eid = await get_latest_event_id(conv_id) if is_streaming else 0
        return {"streaming": is_streaming, "last_event_id": last_eid}

    # ── 工具历史 ──────────────────────────────────────────────────────────────

    async def get_tool_history(self, conv_id: str) -> list:
        return await get_tool_events(conv_id)

    # ── 记忆调试 ──────────────────────────────────────────────────────────────

    async def get_memory_debug(self, conv_id: str) -> dict:
        from rag import retriever as rag_retriever
        from tools import get_tool_names

        conv = memory_store.get(conv_id)
        if not conv:
            return {"error": "对话不存在"}
        lt_count = (
            await rag_retriever.count_by_conv(conv_id) if LONGTERM_MEMORY_ENABLED else -1
        )
        return {
            "total_messages": len(conv.messages),
            "summarised_count": conv.mid_term_cursor,
            "window_count": len(conv.messages) - conv.mid_term_cursor,
            "mid_term_summary": conv.mid_term_summary or "(空)",
            "long_term_stored_pairs": lt_count if LONGTERM_MEMORY_ENABLED else "(已禁用)",
            "active_tools": get_tool_names(),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # 内部方法
    # ══════════════════════════════════════════════════════════════════════════

    async def _cleanup_conversation(self, conv_id: str) -> None:
        """
        完整清理一个对话的所有关联数据：
          1. 停止活跃流
          2. 清理 Redis 状态（stop signal / streaming key）
          3. 清理 RAG 向量
          4. 清理沙箱会话
          5. 删除 DB 数据（无 CASCADE 的表手动删 + conversations 主表级联删其余）
          6. 清理内存缓存
        """
        # 1. 停止活跃流
        await self._stop_active_stream(conv_id)

        # 2. 清理 Redis 状态
        await self._cleanup_redis(conv_id)

        # 3. 清理 RAG 向量（长期记忆）
        if LONGTERM_MEMORY_ENABLED:
            try:
                from rag import retriever as rag_retriever
                await rag_retriever.delete_by_conv(conv_id)
            except Exception as exc:
                logger.warning("RAG 清理失败 conv=%s: %s", conv_id, exc)

        # 4. 清理沙箱会话
        await self._cleanup_sandbox_session(conv_id)

        # 5. 手动删除无 CASCADE 外键的关联表，再删主表（CASCADE 处理 tool_executions + event_log）
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(MessageModel).where(MessageModel.conv_id == conv_id)
            )
            await session.execute(
                sa_delete(ArtifactModel).where(ArtifactModel.conv_id == conv_id)
            )
            await session.execute(
                sa_delete(PlanStepModel).where(PlanStepModel.conv_id == conv_id)
            )
            await session.execute(
                sa_delete(ToolEventModel).where(ToolEventModel.conv_id == conv_id)
            )
            try:
                await session.execute(
                    sa_delete(MessageDetailModel).where(MessageDetailModel.conv_id == conv_id)
                )
            except Exception:
                pass  # 旧表可能不存在
            await session.commit()

        # 删除主表（级联删除 tool_executions + event_log）+ 内存缓存
        await memory_store.delete(conv_id)

    async def _stop_active_stream(self, conv_id: str) -> None:
        """停止对话的活跃流（本 worker + 跨 worker）。"""
        try:
            from graph.runner.stream import _active_sessions
            session = _active_sessions.get(conv_id)
            if session and session._graph_task and not session._graph_task.done():
                session._graph_task.cancel()
        except Exception:
            pass
        try:
            from db.redis_state import publish_stop
            await publish_stop(conv_id)
        except Exception:
            pass

    async def _cleanup_redis(self, conv_id: str) -> None:
        """清理 Redis 中该对话的所有 key。"""
        try:
            from db.redis_state import clear_stop, unregister_streaming
            await asyncio.gather(
                clear_stop(conv_id),
                unregister_streaming(conv_id),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.warning("Redis 清理失败 conv=%s: %s", conv_id, exc)

        # 清理语义缓存中该对话的条目（如果有的话）
        try:
            from cache.factory import get_cache
            cache = get_cache()
            if hasattr(cache, "delete_by_namespace"):
                await cache.delete_by_namespace(conv_id)
        except Exception:
            pass

    async def _cleanup_sandbox_session(self, conv_id: str) -> None:
        """清理沙箱会话（从内存映射中移除）。"""
        try:
            from config import SANDBOX_ENABLED
            if SANDBOX_ENABLED:
                from sandbox.manager import sandbox_manager
                if sandbox_manager.available:
                    sandbox_manager._sessions.pop(conv_id, None)
        except Exception as exc:
            logger.warning("沙箱会话清理失败 conv=%s: %s", conv_id, exc)


# 单例
conversation_service = ConversationService()
