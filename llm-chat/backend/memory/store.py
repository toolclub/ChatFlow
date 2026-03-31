"""
对话存储层：PostgreSQL 持久化 + 内存字典缓存

设计原则：
  - conv.messages 是唯一权威数据源，永不被删除
  - PostgreSQL 作为持久化后端，内存字典 _store 作为读缓存
  - 所有读操作（get / all_conversations）走缓存（保持向后兼容同步接口）
  - 所有写操作（create / save / add_message / delete）为 async，同时更新缓存和 DB
  - 启动时从 PostgreSQL 加载全部对话到内存缓存
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


# ── 读缓存接口（同步，向后兼容）────────────────────────────────────────────────

def get(conv_id: str) -> Optional[Conversation]:
    return _store.get(conv_id)


def all_conversations(client_id: str = "") -> list[Conversation]:
    """返回指定 client 的对话。旧数据（client_id 为空）对所有人可见（向后兼容）。"""
    if not client_id:
        return list(_store.values())
    return [c for c in _store.values() if not c.client_id or c.client_id == client_id]


# ── 初始化 ────────────────────────────────────────────────────────────────────

async def init() -> None:
    """应用启动时调用：从 PostgreSQL 加载全部对话到内存缓存。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ConversationModel))
        conv_rows = result.scalars().all()

        for row in conv_rows:
            msgs_result = await session.execute(
                select(MessageModel)
                .where(MessageModel.conv_id == row.id)
                .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
            )
            msg_rows = msgs_result.scalars().all()
            messages = [
                Message(
                    role=mr.role,
                    content=mr.content,
                    timestamp=mr.created_at,
                    id=mr.id,
                )
                for mr in msg_rows
            ]
            conv = Conversation(
                id=row.id,
                title=row.title,
                system_prompt=row.system_prompt,
                messages=messages,
                mid_term_summary=row.mid_term_summary,
                mid_term_cursor=row.mid_term_cursor,
                created_at=row.created_at,
                updated_at=row.updated_at,
                client_id=row.client_id,
            )
            _store[row.id] = conv

    logger.info("对话存储初始化完成，共加载 %d 个对话", len(_store))


# ── CRUD（异步写操作）─────────────────────────────────────────────────────────

async def create(
    conv_id: str,
    title: str = "新对话",
    system_prompt: str = "",
    client_id: str = "",
) -> Conversation:
    """创建新对话：写入 DB + 更新缓存。"""
    prompt = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    now = time.time()
    conv = Conversation(
        id=conv_id,
        title=title,
        system_prompt=prompt,
        client_id=client_id,
        created_at=now,
        updated_at=now,
    )
    _store[conv_id] = conv

    async with AsyncSessionLocal() as session:
        session.add(ConversationModel(
            id=conv.id,
            title=conv.title,
            system_prompt=conv.system_prompt,
            mid_term_summary="",
            mid_term_cursor=0,
            client_id=conv.client_id,
            created_at=now,
            updated_at=now,
        ))
        await session.commit()

    return conv


async def save(conv: Conversation) -> None:
    """更新对话元数据（title / system_prompt / mid_term_summary / mid_term_cursor）。
    注意：不负责 messages，messages 通过 add_message 单独写入。"""
    conv.updated_at = time.time()
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv.id)
            .values(
                title=conv.title,
                system_prompt=conv.system_prompt,
                mid_term_summary=conv.mid_term_summary,
                mid_term_cursor=conv.mid_term_cursor,
                updated_at=conv.updated_at,
            )
        )
        await session.commit()


async def delete(conv_id: str) -> None:
    """删除对话：从缓存和 DB 中删除（messages / tool_events 级联删除）。"""
    _store.pop(conv_id, None)
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_delete(ConversationModel).where(ConversationModel.id == conv_id)
        )
        await session.commit()


async def add_message(conv_id: str, role: str, content: str) -> None:
    """追加一条消息：写入 DB + 更新缓存，自动更新对话标题（首条用户消息）。"""
    conv = _store.get(conv_id)
    if not conv:
        return

    now = time.time()
    new_title = conv.title
    if conv.title == "新对话" and role == "user":
        new_title = content[:30] + ("..." if len(content) > 30 else "")

    async with AsyncSessionLocal() as session:
        msg_row = MessageModel(conv_id=conv_id, role=role, content=content, created_at=now)
        session.add(msg_row)
        await session.flush()        # 获取自增 ID
        msg_db_id = msg_row.id

        await session.execute(
            sa_update(ConversationModel)
            .where(ConversationModel.id == conv_id)
            .values(updated_at=now, title=new_title)
        )
        await session.commit()

    # 更新内存缓存
    msg = Message(role=role, content=content, timestamp=now, id=msg_db_id)
    conv.messages.append(msg)
    conv.updated_at = now
    conv.title = new_title


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
