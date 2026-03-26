"""
对话存储层：内存状态 + JSON 磁盘持久化
替代原来的 layers/state.py + layers/persistence.py

设计原则：
  - conv.messages 是唯一权威数据源，永不被删除
  - 启动时从磁盘加载到内存字典
  - 每次修改后同步写磁盘（保证崩溃恢复）
  - 向后兼容旧版 JSON 格式（"short_term" 字段）
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from memory.schema import Conversation, Message
from config import CONVERSATIONS_DIR, DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("memory.store")

_store: dict[str, Conversation] = {}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    Path(CONVERSATIONS_DIR).mkdir(parents=True, exist_ok=True)


def _path(conv_id: str) -> str:
    return os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")


# ── 持久化 ────────────────────────────────────────────────────────────────────

def save(conv: Conversation) -> None:
    """将对话序列化为 JSON 并写入磁盘。"""
    conv.updated_at = time.time()
    _ensure_dir()
    data = {
        "id": conv.id,
        "title": conv.title,
        "system_prompt": conv.system_prompt,
        "messages": [
            {"role": m.role, "content": m.content, "timestamp": m.timestamp}
            for m in conv.messages
        ],
        "mid_term_summary": conv.mid_term_summary,
        "mid_term_cursor": conv.mid_term_cursor,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "client_id": conv.client_id,
    }
    with open(_path(conv.id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_all_from_disk() -> list[Conversation]:
    """从磁盘读取所有 .json 文件，返回 Conversation 列表。"""
    _ensure_dir()
    result = []
    for fname in Path(CONVERSATIONS_DIR).glob("*.json"):
        try:
            with open(fname, encoding="utf-8") as f:
                data = json.load(f)
            # 向后兼容：旧版本用 "short_term" 字段
            raw_msgs = data.get("messages", data.get("short_term", []))
            messages = [
                Message(
                    role=m["role"],
                    content=m["content"],
                    timestamp=m.get("timestamp", 0.0),
                )
                for m in raw_msgs
            ]
            conv = Conversation(
                id=data["id"],
                title=data.get("title", "新对话"),
                system_prompt=data.get("system_prompt", ""),
                messages=messages,
                mid_term_summary=data.get("mid_term_summary", ""),
                mid_term_cursor=data.get("mid_term_cursor", 0),
                created_at=data.get("created_at", 0.0),
                updated_at=data.get("updated_at", 0.0),
                client_id=data.get("client_id", ""),
            )
            result.append(conv)
        except Exception as exc:
            logger.error("加载对话文件 %s 失败: %s", fname, exc)
    return result


def _delete_from_disk(conv_id: str) -> None:
    p = Path(_path(conv_id))
    if p.exists():
        p.unlink()


# ── 内存 CRUD ─────────────────────────────────────────────────────────────────

def init() -> None:
    """应用启动时调用：从磁盘加载全部对话到内存。"""
    convs = _load_all_from_disk()
    for conv in convs:
        _store[conv.id] = conv
    logger.info("对话存储初始化完成，共加载 %d 个对话", len(_store))


def get(conv_id: str) -> Optional[Conversation]:
    return _store.get(conv_id)


def all_conversations(client_id: str = "") -> list[Conversation]:
    """返回指定 client 的对话。旧数据（client_id 为空）对所有人可见（向后兼容）。"""
    if not client_id:
        return list(_store.values())
    return [c for c in _store.values() if not c.client_id or c.client_id == client_id]


def create(
    conv_id: str,
    title: str = "新对话",
    system_prompt: str = "",
    client_id: str = "",
) -> Conversation:
    prompt = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    conv = Conversation(id=conv_id, title=title, system_prompt=prompt, client_id=client_id)
    _store[conv_id] = conv
    save(conv)
    return conv


def delete(conv_id: str) -> None:
    _store.pop(conv_id, None)
    _delete_from_disk(conv_id)


def add_message(conv_id: str, role: str, content: str) -> None:
    """追加一条消息，并自动更新对话标题（首条用户消息）。"""
    conv = _store.get(conv_id)
    if not conv:
        return
    conv.messages.append(Message(role=role, content=content))
    if conv.title == "新对话" and role == "user":
        conv.title = content[:30] + ("..." if len(content) > 30 else "")
    save(conv)
