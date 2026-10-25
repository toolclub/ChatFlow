"""
事件流持久化层 — 替代纯内存 event_buffer

核心操作：
  append_event()      — 写入单条事件到 event_log
  append_events()     — 批量写入（性能优化）
  get_events_since()  — 从某个 event_id 之后读取事件（SSE 重连用）
  get_all_events()    — 获取对话的全部事件（full-state 用）
  cleanup_events()    — 清理老事件（定期任务）
"""
import logging
import time

from sqlalchemy import select, delete as sa_delete

from db.database import AsyncSessionLocal
from db.models import EventLogModel

logger = logging.getLogger("db.event_store")


async def append_event(
    conv_id: str,
    event_type: str,
    event_data: dict | None = None,
    sse_string: str = "",
    message_id: str = "",
) -> int:
    """写入单条事件，返回自增 ID。"""
    async with AsyncSessionLocal() as session:
        row = EventLogModel(
            conv_id=conv_id,
            message_id=message_id,
            event_type=event_type,
            event_data=event_data or {},
            sse_string=sse_string,
            created_at=time.time(),
        )
        session.add(row)
        await session.flush()
        eid = row.id
        await session.commit()
    return eid


async def append_events_batch(events: list[dict]) -> list[int]:
    """批量写入事件（性能优化，减少 DB round-trips）。"""
    if not events:
        return []
    now = time.time()
    async with AsyncSessionLocal() as session:
        rows = []
        for e in events:
            rows.append(EventLogModel(
                conv_id=e["conv_id"],
                message_id=e.get("message_id", ""),
                event_type=e["event_type"],
                event_data=e.get("event_data", {}),
                sse_string=e.get("sse_string", ""),
                created_at=now,
            ))
        session.add_all(rows)
        await session.flush()
        ids = [row.id for row in rows]
        await session.commit()
    return ids


async def get_events_since(conv_id: str, after_id: int = 0, message_id: str = "", limit: int = 500) -> list[dict]:
    """
    获取 event_id > after_id 的事件（SSE 重连用）。

    message_id: 可选过滤条件，只返回指定 message 的事件（多轮对话恢复时避免混入旧轮事件）。
    """
    async with AsyncSessionLocal() as session:
        query = (
            select(EventLogModel)
            .where(EventLogModel.conv_id == conv_id)
            .where(EventLogModel.id > after_id)
        )
        if message_id:
            query = query.where(EventLogModel.message_id == message_id)
        query = query.order_by(EventLogModel.id.asc()).limit(max(1, min(limit, 1000)))
        result = await session.execute(query)
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "event_data": r.event_data,
            "sse_string": r.sse_string,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def get_latest_event_id(conv_id: str) -> int:
    """获取对话最新的 event_id（前端据此决定从哪里恢复）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EventLogModel.id)
            .where(EventLogModel.conv_id == conv_id)
            .order_by(EventLogModel.id.desc())
            .limit(1)
        )
        row = result.scalar()
    return row or 0


async def cleanup_completed_events(conv_id: str, keep_terminal: bool = True) -> int:
    """清理已完成对话的事件（保留 done/stopped/error 终止事件）。"""
    async with AsyncSessionLocal() as session:
        if keep_terminal:
            result = await session.execute(
                sa_delete(EventLogModel)
                .where(EventLogModel.conv_id == conv_id)
                .where(EventLogModel.event_type.notin_(["done", "stopped", "error"]))
            )
        else:
            result = await session.execute(
                sa_delete(EventLogModel)
                .where(EventLogModel.conv_id == conv_id)
            )
        count = result.rowcount
        await session.commit()
    return count
