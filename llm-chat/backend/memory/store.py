"""
对话存储层：PostgreSQL 持久化 + 内存字典缓存

设计原则：
  - conv.messages 是唯一权威数据源，永不被删除
  - PostgreSQL 作为持久化后端，内存字典 _store 作为读缓存
  - 所有读操作（get / all_conversations）走缓存（保持向后兼容同步接口）
  - 所有写操作（create / save / add_message / delete）为 async，同时更新缓存和 DB
  - 执行入口按需从 PostgreSQL 加载对话；UI 读取不改变推理缓存
"""
import logging
import time
from typing import Optional

from sqlalchemy import select, update as sa_update, delete as sa_delete

from db.database import AsyncSessionLocal
from db.models import ConversationModel, MessageModel
from memory.schema import Conversation, Message
from config import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("memory.store")

_store: dict[str, Conversation] = {}


# ── 读缓存接口（同步，供图执行内部使用）────────────────────────────────────────

def get(conv_id: str) -> Optional[Conversation]:
    return _store.get(conv_id)


def all_conversations(client_id: str = "", user_id: str = "") -> list[Conversation]:
    """返回指定 client 或 user 的对话（内存缓存）。"""
    if user_id:
        return [c for c in _store.values() if c.user_id == user_id]
    if client_id:
        return [c for c in _store.values() if c.client_id == client_id and not c.user_id]
    return []


# ── DB 直查接口（多 worker 安全，供 API 端点使用）────────────────────────────────

from db.models import ConversationModel, MessageModel, ToolExecutionModel

# ... (imports unchanged)

async def db_get_conversation_meta(conv_id: str) -> Optional[dict]:
    """Read ownership and lifecycle without loading a potentially long transcript."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(
            ConversationModel.id, ConversationModel.title, ConversationModel.user_id,
            ConversationModel.client_id, ConversationModel.status, ConversationModel.updated_at,
        ).where(ConversationModel.id == conv_id))).mappings().first()
        return dict(row) if row else None


async def db_get_conversation_view(conv_id: str, *, include_tools: bool = True) -> Optional[dict]:
    """Read UI data directly, without reconstructing or replacing model history.

    Full-state already loads detailed tool executions; it passes include_tools=False
    and reconstructs the compatibility tool_calls field from that one query.
    """
    async with AsyncSessionLocal() as session:
        meta = (await session.execute(select(
            ConversationModel.id, ConversationModel.title, ConversationModel.updated_at,
            ConversationModel.system_prompt, ConversationModel.core_memory,
            ConversationModel.mid_term_summary, ConversationModel.user_id,
            ConversationModel.client_id, ConversationModel.status,
        ).where(ConversationModel.id == conv_id))).mappings().first()
        if meta is None:
            return None
        rows = (await session.execute(select(
            MessageModel.role, MessageModel.content, MessageModel.thinking,
            MessageModel.thinking_segments, MessageModel.message_id,
            MessageModel.stream_completed, MessageModel.stream_buffer, MessageModel.images,
            MessageModel.tool_summary, MessageModel.step_summary, MessageModel.prompt_tokens,
            MessageModel.completion_tokens, MessageModel.reasoning_tokens,
            MessageModel.clarification_data, MessageModel.created_at.label("timestamp"),
        ).where(MessageModel.conv_id == conv_id)
          .order_by(MessageModel.created_at.asc(), MessageModel.id.asc()))).mappings().all()
        tool_calls: dict[str, list[dict]] = {}
        if include_tools:
            tools = (await session.execute(select(
                ToolExecutionModel.id, ToolExecutionModel.message_id,
                ToolExecutionModel.tool_name, ToolExecutionModel.tool_input,
            ).where(ToolExecutionModel.conv_id == conv_id)
              .order_by(ToolExecutionModel.sequence_number.asc(), ToolExecutionModel.id.asc()))).mappings().all()
            for tool in tools:
                tool_calls.setdefault(tool["message_id"], []).append({
                    "id": f"call_{tool['id']}", "name": tool["tool_name"],
                    "args": tool["tool_input"], "type": "tool_call",
                })
    return {**dict(meta), "messages": [
        {**dict(row), "tool_calls": tool_calls.get(row["message_id"], [])} for row in rows
    ]}


async def get_latest_assistant_message_id(conv_id: str) -> str:
    """Resolve an omitted replay round without loading a conversation transcript."""
    async with AsyncSessionLocal() as session:
        message_id = await session.scalar(select(MessageModel.message_id).where(
            MessageModel.conv_id == conv_id, MessageModel.role == "assistant",
        ).order_by(MessageModel.created_at.desc(), MessageModel.id.desc()).limit(1))
    return message_id or ""


async def db_get_conversation(conv_id: str) -> Optional[dict]:
    """从 DB 直接读取对话（跨 worker 一致性）。返回 dict 或 None。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(ConversationModel, conv_id)
        if not row:
            return None

        msgs_result = await session.execute(
            select(MessageModel)
            .where(MessageModel.conv_id == conv_id)
            .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        )
        msg_rows = msgs_result.scalars().all()

        # 批量获取本会话的所有工具调用记录
        tool_execs_result = await session.execute(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.conv_id == conv_id)
            .order_by(ToolExecutionModel.sequence_number.asc())
        )
        tool_rows = tool_execs_result.scalars().all()
        
        # 按 message_id 分组工具调用（LangChain 格式）
        from collections import defaultdict
        msg_tool_calls = defaultdict(list)
        for tr in tool_rows:
            msg_tool_calls[tr.message_id].append({
                "id": f"call_{tr.id}", # 还原 ID
                "name": tr.tool_name,
                "args": tr.tool_input,
                "type": "tool_call"
            })

        # 同时刷新本 worker 的内存缓存
        messages_obj = [
            Message(
                role=mr.role, content=mr.content, timestamp=mr.created_at, id=mr.id,
                tool_summary=getattr(mr, "tool_summary", "") or "",
                step_summary=getattr(mr, "step_summary", "") or "",
                thinking=getattr(mr, "thinking", "") or "",
                thinking_segments=list(getattr(mr, "thinking_segments", []) or []),
                tool_calls=msg_tool_calls.get(getattr(mr, "message_id", ""), []),
                prompt_tokens=getattr(mr, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(mr, "completion_tokens", 0) or 0,
                reasoning_tokens=getattr(mr, "reasoning_tokens", 0) or 0,
            )
            for mr in msg_rows
        ]
        conv = Conversation(
            id=row.id, title=row.title, system_prompt=row.system_prompt,
            core_memory=dict(getattr(row, "core_memory", {}) or {}),
            messages=messages_obj, mid_term_summary=row.mid_term_summary,
            mid_term_cursor=row.mid_term_cursor, created_at=row.created_at,
            updated_at=row.updated_at, client_id=row.client_id,
            user_id=getattr(row, "user_id", "") or "",
            status=getattr(row, "status", "active") or "active",
        )
        _store[conv_id] = conv  # 刷新缓存

        return {
            "id": row.id,
            "title": row.title,
            "updated_at": row.updated_at,
            "system_prompt": row.system_prompt,
            "core_memory": dict(getattr(row, "core_memory", {}) or {}),
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "thinking": m.thinking,
                    "thinking_segments": m.thinking_segments,
                    "tool_calls": m.tool_calls,
                    "message_id": getattr(mr, "message_id", "") or "",
                    "stream_completed": getattr(mr, "stream_completed", True),
                    "stream_buffer": getattr(mr, "stream_buffer", "") or "",
                    "images": getattr(mr, "images", []) or [],
                    "tool_summary": m.tool_summary,
                    "step_summary": m.step_summary,
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "reasoning_tokens": m.reasoning_tokens,
                    "clarification_data": getattr(mr, "clarification_data", {}) or {},
                    "timestamp": m.timestamp,
                }
                for m, mr in zip(messages_obj, msg_rows)
            ],
            "mid_term_summary": row.mid_term_summary,
            "user_id": getattr(row, "user_id", "") or "",
            "client_id": row.client_id or "",
            "status": getattr(row, "status", "active") or "active",
        }


async def db_list_conversations(client_id: str = "", user_id: str = "") -> list[dict]:
    """从 DB 直接读取对话列表（跨 worker 一致性）。"""
    if not client_id and not user_id:
        return []
    async with AsyncSessionLocal() as session:
        query = select(ConversationModel).order_by(ConversationModel.updated_at.desc())
        if user_id:
            query = query.where(ConversationModel.user_id == user_id)
        elif client_id:
            query = query.where(
                ConversationModel.client_id == client_id,
                ConversationModel.user_id == ""
            )
        result = await session.execute(query)
        rows = result.scalars().all()

    return [
        {"id": r.id, "title": r.title, "updated_at": r.updated_at}
        for r in rows
    ]


# ── 初始化 ────────────────────────────────────────────────────────────────────

async def init() -> None:
    """Start invalidation without loading every user's history into each worker."""
    logger.info("对话存储初始化完成，历史按执行需要加载")

    async def _on_invalidate(conv_id: str):
        # UI reads do not need this cache. Execution entry points hydrate from
        # DB, so invalidation must not fan out full-history reads to all workers.
        conv = _store.get(conv_id)
        if conv is None or conv.status == "streaming":
            return
        # Background extraction retains its own Conversation reference. Keep
        # the active graph's history stable until its next explicit hydration.
        from graph.runner.stream import is_streaming_in_worker
        if not is_streaming_in_worker(conv_id):
            _store.pop(conv_id, None)

    try:
        from db.redis_state import start_cache_invalidation_listener
        await start_cache_invalidation_listener(_on_invalidate)
    except Exception as exc:
        logger.warning("Redis 缓存失效监听启动失败（降级为本地缓存）: %s", exc)


# ── CRUD（异步写操作）─────────────────────────────────────────────────────────

async def create(
    conv_id: str,
    title: str = "新对话",
    system_prompt: str = "",
    client_id: str = "",
    user_id: str = "",
) -> Conversation:
    """创建新对话：写入 DB + 更新缓存。"""
    prompt = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    now = time.time()
    conv = Conversation(
        id=conv_id,
        title=title,
        system_prompt=prompt,
        client_id=client_id,
        user_id=user_id,
        created_at=now,
        updated_at=now,
    )
    _store[conv_id] = conv

    async with AsyncSessionLocal() as session:
        session.add(ConversationModel(
            id=conv.id,
            title=conv.title,
            system_prompt=conv.system_prompt,
            core_memory=conv.core_memory,
            mid_term_summary="",
            mid_term_cursor=0,
            client_id=conv.client_id,
            user_id=conv.user_id,
            status="active",
            created_at=now,
            updated_at=now,
        ))
        await session.commit()

    return conv


async def save(conv: Conversation) -> None:
    """更新对话元数据（title / system_prompt / core_memory / mid_term_summary / mid_term_cursor）。
    注意：不负责 messages，messages 通过 add_message 单独写入。
    写完通过 Redis pub/sub 通知其他 worker 失效本地缓存，保证跨 worker 一致性。"""
    conv.updated_at = time.time()
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv.id)
            .values(
                title=conv.title,
                system_prompt=conv.system_prompt,
                core_memory=conv.core_memory,
                mid_term_summary=conv.mid_term_summary,
                mid_term_cursor=conv.mid_term_cursor,
                updated_at=conv.updated_at,
            )
        )
        await session.commit()
    await _notify_cache_invalidation(conv.id)


async def update_conversation_fields(
    conv_id: str, *, title: str | None = None, system_prompt: str | None = None,
) -> bool:
    """Patch only requested fields without replacing an active model's history."""
    values = {"updated_at": time.time()}
    if title is not None:
        values["title"] = title
    if system_prompt is not None:
        values["system_prompt"] = system_prompt
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_update(ConversationModel).where(
            ConversationModel.id == conv_id,
        ).values(**values).returning(ConversationModel.id))
        exists = result.scalar_one_or_none() is not None
        await session.commit()
    if exists:
        cached = _store.get(conv_id)
        if cached:
            for field, value in values.items():
                setattr(cached, field, value)
        await _notify_cache_invalidation(conv_id)
    return exists


async def delete(conv_id: str) -> None:
    """Atomically delete idle conversations under the same lock as turn admission."""
    from db.models import (
        ArtifactModel, PlanStepModel, ToolEventModel, MessageDetailModel, EventLogModel,
    )
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(ConversationModel).where(ConversationModel.id == conv_id).with_for_update()
        )).scalar_one_or_none()
        if row and row.status == "streaming":
            raise TimeoutError("对话仍在运行，请稍后重试删除")
        # Legacy tables have no reliable ON DELETE CASCADE constraint.
        for model in (EventLogModel, ToolExecutionModel, MessageDetailModel,
                      ToolEventModel, PlanStepModel, ArtifactModel, MessageModel):
            await session.execute(sa_delete(model).where(model.conv_id == conv_id))
        await session.execute(sa_delete(ConversationModel).where(ConversationModel.id == conv_id))
        await session.commit()
    _store.pop(conv_id, None)
    await _notify_cache_invalidation(conv_id)


async def add_message(
    conv_id: str, role: str, content: str, update_db_id: int = 0,
    tool_summary: str = "", step_summary: str = "",
    usage: dict | None = None,
) -> None:
    """
    追加一条消息：写入 DB + 更新缓存。
    """
    conv = _store.get(conv_id)
    if not conv:
        logger.warning("add_message 跳过：对话 %s 不在缓存中（可能已被删除）", conv_id)
        return

    now = time.time()
    new_title = conv.title
    if conv.title == "新对话" and role == "user":
        new_title = content[:30] + ("..." if len(content) > 30 else "")

    msg_db_id = update_db_id
    u = usage or {}
    p_tokens = u.get("prompt_tokens", 0)
    c_tokens = u.get("completion_tokens", 0)
    r_tokens = u.get("reasoning_tokens", 0)

    async with AsyncSessionLocal() as session:
        if update_db_id > 0:
            # UPDATE 已有行
            values: dict = {
                "content": content,
                "stream_completed": True,
                "stream_buffer": "",
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "reasoning_tokens": r_tokens,
            }
            if tool_summary:
                values["tool_summary"] = tool_summary
            if step_summary:
                values["step_summary"] = step_summary
            await session.execute(
                sa_update(MessageModel)
                .where(MessageModel.id == update_db_id)
                .values(**values)
            )
        else:
            # INSERT 新行
            msg_row = MessageModel(
                conv_id=conv_id, role=role, content=content,
                tool_summary=tool_summary, step_summary=step_summary,
                prompt_tokens=p_tokens, completion_tokens=c_tokens,
                reasoning_tokens=r_tokens, created_at=now,
            )
            session.add(msg_row)
            await session.flush()
            msg_db_id = msg_row.id

        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv_id)
            .values(updated_at=now, title=new_title)
        )
        await session.commit()

    # 追加到内存缓存
    msg = Message(
        role=role, content=content, timestamp=now, id=msg_db_id,
        tool_summary=tool_summary, step_summary=step_summary,
        tool_calls=[], # 新消息初始工具调用为空，后续由 ToolExecution 关联
        prompt_tokens=p_tokens, completion_tokens=c_tokens,
        reasoning_tokens=r_tokens,
    )
    conv.messages.append(msg)
    conv.updated_at = now
    conv.title = new_title


async def _notify_cache_invalidation(conv_id: str) -> None:
    """通知其他 worker 失效本地缓存（Redis pub/sub）。"""
    try:
        from db.redis_state import publish_cache_invalidation
        await publish_cache_invalidation(conv_id)
    except Exception:
        pass  # Redis 不可用时降级


async def update_status(conv_id: str, status: str) -> None:
    """
    将对话状态持久化到 DB + 通知缓存失效。

    状态校验由调用方的状态机实例负责（StreamSession._conv_sm），
    此函数只做持久化，不做校验。
    """
    conv = _store.get(conv_id)
    if conv:
        conv.status = status
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv_id)
            .values(status=status, updated_at=time.time())
        )
        await session.commit()
    await _notify_cache_invalidation(conv_id)


async def create_message_immediate(
    conv_id: str, role: str, content: str, message_id: str = "",
    thinking: str = "", images: list | None = None, stream_completed: bool = True,
    prompt_tokens: int = 0, completion_tokens: int = 0, reasoning_tokens: int = 0,
) -> int:
    """
    立即写入消息到 DB（仅 DB，不写内存缓存）。返回 DB 自增 ID。

    重要：不更新 conv.messages 内存缓存！
    因为图执行期间会读取 conv.messages 构建 LLM 上下文，
    如果提前把当前轮的 user/assistant 加进去，会破坏
    tool_call → tool_result 的消息配对顺序，导致 MiniMax 等 API 报错。
    内存缓存由 save_response_node 在图执行完成后统一更新。
    """
    conv = _store.get(conv_id)
    now = time.time()
    seq = len(conv.messages) if conv else 0

    new_title = None
    if conv and conv.title == "新对话" and role == "user":
        new_title = content[:30] + ("..." if len(content) > 30 else "")

    async with AsyncSessionLocal() as session:
        msg_row = MessageModel(
            conv_id=conv_id, role=role, content=content,
            message_id=message_id, thinking=thinking,
            stream_completed=stream_completed,
            sequence_number=seq,
            images=images or [],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            created_at=now,
        )
        session.add(msg_row)
        await session.flush()
        db_id = msg_row.id

        update_vals = {"updated_at": now}
        if new_title:
            update_vals["title"] = new_title
        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv_id)
            .values(**update_vals)
        )
        await session.commit()

    # 只更新标题到缓存（不追加消息）
    if conv and new_title:
        conv.title = new_title

    return db_id


async def update_message_streaming(
    msg_db_id: int,
    thinking: str | None = None,
    stream_buffer: str | None = None,
    stream_completed: bool | None = None,
    thinking_segments: list | None = None,
) -> None:
    """
    更新正在流式生成的消息（定期刷新 thinking/buffer/segments）。

    参数语义：None = 不更新该字段；"" / [] = 显式清空。
    thinking_segments 传入时为当前完整段列表（全量覆盖，调用方维护累积）。
    """
    values: dict = {}
    if stream_completed is not None:
        values["stream_completed"] = stream_completed
    if thinking is not None:
        values["thinking"] = thinking
    if stream_buffer is not None:
        values["stream_buffer"] = stream_buffer
    if thinking_segments is not None:
        values["thinking_segments"] = thinking_segments
    if not values:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(MessageModel)
            .where(MessageModel.id == msg_db_id)
            .values(**values)
        )
        await session.commit()


async def finalize_message(
    msg_db_id: int, content: str, thinking: str = "",
    thinking_segments: list | None = None,
) -> None:
    """消息生成完成：写入最终 content，标记 stream_completed=True，清空 buffer。"""
    values: dict = {
        "content": content,
        "thinking": thinking,
        "stream_buffer": "",
        "stream_completed": True,
    }
    if thinking_segments is not None:
        values["thinking_segments"] = thinking_segments
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(MessageModel)
            .where(MessageModel.id == msg_db_id)
            .values(**values)
        )
        await session.commit()

    # 更新内存缓存中的 content
    # (找到对应消息并更新，但不强依赖)
    for conv in _store.values():
        for msg in conv.messages:
            if msg.id == msg_db_id:
                msg.content = content
                return


async def clear_message_summaries(msg_id: int) -> None:
    """清空消息的 tool_summary 和 step_summary（压缩后不再需要）。"""
    if msg_id <= 0:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(MessageModel)
            .where(MessageModel.id == msg_id)
            .values(tool_summary="", step_summary="")
        )
        await session.commit()


async def update_message_content(msg_id: int, new_content: str) -> None:
    """更新指定消息内容（压缩时将工具调用记录替换为 [old tools call] 占位符）。"""
    if msg_id <= 0:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(MessageModel)
            .where(MessageModel.id == msg_id)
            .values(content=new_content)
        )
        await session.commit()


async def try_start_stream(conv_id: str) -> bool:
    """Serialize new turns across workers using the conversation row lock."""
    from fsm.conversation import ConversationSM
    async with AsyncSessionLocal() as session:
        row = await session.scalar(select(ConversationModel).where(ConversationModel.id == conv_id).with_for_update())
        if not row or row.status == "streaming":
            return False
        sm = ConversationSM.from_db_status(row.status, conv_id=conv_id)
        sm.send_event("streaming")
        row.status = sm.current_value
        row.updated_at = time.time()
        await session.commit()
    return True


async def get_message_stream_state(conv_id: str, message_id: str) -> Optional[dict]:
    """Lightweight replay polling; do not rebuild or overwrite the graph cache."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(MessageModel.stream_completed, ConversationModel.status, ConversationModel.updated_at)
            .join(ConversationModel, ConversationModel.id == MessageModel.conv_id)
            .where(MessageModel.conv_id == conv_id, MessageModel.message_id == message_id)
        )).first()
        return dict(row._mapping) if row else None


async def recover_interrupted_stream(conv_id: str, expected_updated_at: float) -> bool:
    """Preserve partial results and close orphaned state after liveness is checked."""
    from fsm.conversation import ConversationSM
    from fsm.tool_execution import ToolExecutionSM
    from fsm.sse_events import SSEEventType
    from db.models import EventLogModel
    import json

    async with AsyncSessionLocal() as session:
        conv = await session.scalar(select(ConversationModel).where(ConversationModel.id == conv_id).with_for_update())
        if not conv or conv.status != "streaming" or conv.updated_at != expected_updated_at:
            return False
        sm = ConversationSM.from_db_status(conv.status, conv_id=conv_id)
        sm.send_event("error")
        conv.status = sm.current_value
        conv.updated_at = time.time()
        messages = (await session.scalars(select(MessageModel).where(
            MessageModel.conv_id == conv_id, MessageModel.role == "assistant",
            MessageModel.stream_completed.is_(False),
        ))).all()
        for message in messages:
            message.content = message.content or message.stream_buffer or ""
            message.stream_buffer = ""
            message.stream_completed = True
            payload = {SSEEventType.ERROR.value: "执行进程已中断，已保留部分结果，可发送消息继续。", "can_continue": True}
            session.add(EventLogModel(conv_id=conv_id, message_id=message.message_id,
                                      event_type=SSEEventType.ERROR.value, event_data=payload,
                                      sse_string="data: " + json.dumps(payload, ensure_ascii=False) + "\n\n",
                                      created_at=time.time()))
        tools = (await session.scalars(select(ToolExecutionModel).where(
            ToolExecutionModel.conv_id == conv_id, ToolExecutionModel.status == "running",
        ))).all()
        for tool in tools:
            tool_sm = ToolExecutionSM()
            tool_sm.send_event("error")
            tool.status = tool_sm.current_value
        await session.commit()
    logger.info("已恢复中断对话 | conv=%s | messages=%d", conv_id, len(messages))
    await _notify_cache_invalidation(conv_id)
    return True
