"""
Layer 7 – Persistence
Checkpoint: save, load, and delete Conversation state to/from disk.
Acts as the durable store that survives process restarts.
"""
import json
import os
import time
from dataclasses import asdict

from layers.memory import Conversation, Message

CONVERSATIONS_DIR = "./conversations"
os.makedirs(CONVERSATIONS_DIR, exist_ok=True)


def _path(conv_id: str) -> str:
    return os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")


def save(conv: Conversation) -> None:
    conv.updated_at = time.time()
    data = {
        "id": conv.id,
        "title": conv.title,
        "system_prompt": conv.system_prompt,
        "messages": [asdict(m) for m in conv.messages],
        "mid_term_summary": conv.mid_term_summary,
        "mid_term_cursor": conv.mid_term_cursor,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
    }
    with open(_path(conv.id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_all() -> dict[str, Conversation]:
    conversations: dict[str, Conversation] = {}
    for fname in os.listdir(CONVERSATIONS_DIR):
        if not fname.endswith(".json"):
            continue
        filepath = os.path.join(CONVERSATIONS_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # backward compat: old files used "short_term" key
            raw_msgs = data.get("messages") or data.get("short_term", [])
            conv = Conversation(
                id=data["id"],
                title=data.get("title", "新对话"),
                system_prompt=data.get("system_prompt", ""),
                messages=[Message(**m) for m in raw_msgs],
                mid_term_summary=data.get("mid_term_summary", ""),
                mid_term_cursor=data.get("mid_term_cursor", 0),
                created_at=data.get("created_at", 0),
                updated_at=data.get("updated_at", 0),
            )
            conversations[conv.id] = conv
        except Exception:
            pass
    return conversations


def delete(conv_id: str) -> None:
    path = _path(conv_id)
    if os.path.exists(path):
        os.remove(path)
