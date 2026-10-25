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
import time
import uuid

from memory import store as memory_store
from memory.tool_events import get_tool_events
from db.artifact_store import get_artifact_meta_list
from db.plan_store import get_latest_plan_for_conv
from db.tool_store import get_tool_executions_for_conv
from db.event_store import get_latest_event_id
from config import LONGTERM_MEMORY_ENABLED, DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("services.conversation")


class ConversationService:
    """对话管理服务"""

    # ── 列表 / 单查 ──────────────────────────────────────────────────────────

    async def list_conversations(self, client_id: str = "", user_id: str = "") -> list[dict]:
        return await memory_store.db_list_conversations(client_id, user_id)

    async def get_conversation(self, conv_id: str) -> dict | None:
        return await memory_store.db_get_conversation_view(conv_id)

    # ── 创建 ─────────────────────────────────────────────────────────────────

    async def create_conversation(
        self, title: str = "新对话", system_prompt: str = "", client_id: str = "", user_id: str = "",
    ) -> dict:
        conv_id = str(uuid.uuid4())[:8]
        conv = await memory_store.create(
            conv_id=conv_id,
            title=title or "新对话",
            system_prompt=system_prompt or "",
            client_id=client_id,
            user_id=user_id,
        )
        return {"id": conv.id, "title": conv.title}

    # ── 更新 ─────────────────────────────────────────────────────────────────

    async def update_conversation(
        self, conv_id: str, title: str | None = None, system_prompt: str | None = None,
    ) -> dict:
        updated = await memory_store.update_conversation_fields(
            conv_id, title=title, system_prompt=system_prompt,
        )
        return {"ok": True} if updated else {"error": "对话不存在"}

    # ── 单个删除（完整清理） ─────────────────────────────────────────────────

    async def delete_conversation(self, conv_id: str) -> dict:
        await self._cleanup_conversation(conv_id)
        return {"ok": True}

    # ── 批量删除（完整清理） ─────────────────────────────────────────────────

    async def batch_delete_conversations(self, conv_ids: list[str]) -> dict:
        if not conv_ids:
            return {"ok": True, "deleted": 0, "deleted_ids": [], "errors": []}

        conv_ids = list(dict.fromkeys(conv_ids))
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

        return {"ok": not errors, "deleted": deleted, "deleted_ids": [
            cid for cid, result in zip(conv_ids, results) if not isinstance(result, Exception)
        ], "errors": errors}

    # ── 完整状态查询 ──────────────────────────────────────────────────────────

    async def get_full_state(self, conv_id: str) -> dict:
        from db.plan_store import get_all_plans_for_conv
        # Independent UI reads use separate sessions. Tools are read only once,
        # and a browser refresh never replaces the model's in-flight history.
        conv_data, tool_execs, all_plans, artifacts, last_event_id = await asyncio.gather(
            memory_store.db_get_conversation_view(conv_id, include_tools=False),
            get_tool_executions_for_conv(conv_id),
            get_all_plans_for_conv(conv_id),
            get_artifact_meta_list(conv_id),
            get_latest_event_id(conv_id),
        )
        if not conv_data:
            return {"error": "对话不存在"}
        original_status = conv_data.get("status")
        conv_data = await self.reconcile_stream_state(conv_id, conv_data, refresh_cache=False)
        if conv_data.get("status") != original_status:
            # Orphan recovery finalizes running tools as well as messages.
            tool_execs = await get_tool_executions_for_conv(conv_id)

        has_streaming = conv_data.get("status") == "streaming" and any(
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
                msg["tool_calls"] = [{
                    "id": f"call_{tool['id']}", "name": tool["tool_name"],
                    "args": tool["tool_input"], "type": "tool_call",
                } for tool in sorted(tool_by_msg[msg_id], key=lambda t: (t.get("sequence_number", 0), t["id"]))]
            if msg_id and msg_id in artifact_by_msg:
                msg["artifacts"] = artifact_by_msg[msg_id]
            enriched_messages.append(msg)

        orphan_artifacts = [a for a in artifacts if not a.get("message_id")]

        return {
            "id": conv_data["id"],
            "title": conv_data["title"],
            "status": conv_data.get("status", "active"),
            "messages": enriched_messages,
            "plan": all_plans[-1] if all_plans else None,  # 向后兼容：取最后一个计划
            "plans": all_plans,                            # 新增：所有计划，供前端按 message_id 匹配
            "artifacts": orphan_artifacts,
            "has_streaming": has_streaming,
            "can_continue": conv_data.get("status") in {"active", "error"} and any(
                m.get("role") == "assistant" for m in conv_data["messages"]
            ),
            "last_event_id": last_event_id,
        }

    # ── 流式状态 ──────────────────────────────────────────────────────────────

    async def get_streaming_status(self, conv_id: str) -> dict:
        conv_data = await memory_store.db_get_conversation_meta(conv_id)
        if not conv_data:
            return {"streaming": False, "last_event_id": 0}
        conv_data = await self.reconcile_stream_state(conv_id, conv_data, refresh_cache=False)
        is_streaming = conv_data.get("status") == "streaming"
        last_eid = await get_latest_event_id(conv_id) if is_streaming else 0
        return {"streaming": is_streaming, "last_event_id": last_eid}

    async def reconcile_stream_state(self, conv_id: str, conv_data: dict | None = None, *, refresh_cache: bool = True) -> dict:
        conv_data = conv_data or await memory_store.db_get_conversation_meta(conv_id) or {}
        if conv_data.get("status") != "streaming":
            return conv_data
        from graph.runner.stream import _active_sessions
        if conv_id in _active_sessions or time.time() - conv_data.get("updated_at", time.time()) < 30:
            return conv_data
        try:
            from db.redis_state import is_streaming
            alive = await asyncio.wait_for(is_streaming(conv_id), timeout=2)
        except Exception as exc:
            # Redis being unavailable is not evidence that another worker died.
            logger.warning("无法确认流存活状态 | conv=%s | %s", conv_id, exc)
            return conv_data
        if not alive and await memory_store.recover_interrupted_stream(conv_id, expected_updated_at=conv_data["updated_at"]):
            if refresh_cache:
                return await memory_store.db_get_conversation(conv_id) or conv_data
            if "messages" in conv_data:
                return await memory_store.db_get_conversation_view(conv_id, include_tools=False) or conv_data
            return await memory_store.db_get_conversation_meta(conv_id) or conv_data
        return conv_data

    # ── 工具历史 ──────────────────────────────────────────────────────────────

    async def get_tool_history(self, conv_id: str) -> list:
        return await get_tool_events(conv_id)

    # ── 记忆调试 ──────────────────────────────────────────────────────────────

    async def get_memory_debug(self, conv_id: str) -> dict:
        from rag import retriever as rag_retriever
        from tools import get_tool_names

        conv = memory_store.get(conv_id)
        if not conv:
            await memory_store.db_get_conversation(conv_id)
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
        """Stop the owner and atomically delete DB rows before clearing stop keys."""
        await self._stop_active_stream(conv_id)
        await memory_store.delete(conv_id)
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

    async def _stop_active_stream(self, conv_id: str, timeout: float = 10.0) -> None:
        """Wait for persistence, leaving stop signals intact on timeout.

        The graph's consumer still owns final writes after graph cancellation.
        Never cancel that consumer to make a destructive cleanup proceed.
        """
        from graph.runner.stream import _active_sessions
        from db.redis_state import publish_stop

        async def stop_and_wait():
            local = _active_sessions.get(conv_id)
            if local:
                local.stop_event.set()
                if local._graph_task and not local._graph_task.done():
                    local._graph_task.cancel()
            meta = await memory_store.db_get_conversation_meta(conv_id)
            if meta and meta.get("status") == "streaming":
                try:
                    await asyncio.wait_for(publish_stop(conv_id), timeout=2)
                except Exception as exc:
                    logger.warning("停止信号发布失败 | conv=%s | %s", conv_id, exc)
            tasks = [task for task in (
                getattr(local, "_graph_task", None), getattr(local, "_consumer_task", None),
            ) if task and not task.done()]
            if tasks:
                # Cancelling this waiter never cancels the finalizer.
                await asyncio.wait(tasks)
            while True:
                meta = await memory_store.db_get_conversation_meta(conv_id)
                if not meta or meta.get("status") != "streaming":
                    return
                meta = await self.reconcile_stream_state(conv_id, meta, refresh_cache=False)
                if meta.get("status") != "streaming":
                    return
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(stop_and_wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("任务仍在停止并保存结果，请稍后重试删除") from exc

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
        except Exception as exc:
            logger.warning("语义缓存清理失败 conv=%s: %s", conv_id, exc)

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
