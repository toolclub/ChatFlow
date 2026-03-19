"""
Layer 5 – State
Working memory: holds all active Conversation objects in-process.
Analogous to RAM / OS process state – lives only while the server is running.
"""
from typing import Optional
from layers.memory import Conversation


class StateManager:
    """In-memory store for runtime conversation state."""

    def __init__(self):
        self._store: dict[str, Conversation] = {}

    def get(self, conv_id: str) -> Optional[Conversation]:
        return self._store.get(conv_id)

    def set(self, conv: Conversation) -> None:
        self._store[conv.id] = conv

    def remove(self, conv_id: str) -> None:
        self._store.pop(conv_id, None)

    def all(self) -> list[Conversation]:
        return list(self._store.values())

    def load_from(self, conversations: dict[str, Conversation]) -> None:
        """Bulk-load conversations recovered from persistent storage."""
        self._store.update(conversations)
