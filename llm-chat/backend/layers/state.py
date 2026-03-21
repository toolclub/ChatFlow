"""
第 5 层 – State（状态）
工作记忆：在进程内保存所有活跃的 Conversation 对象。
类比操作系统的内存 / 进程状态——仅在服务运行期间存在。
author: leizihao
email: lzh19162600626@gmail.com
"""
from typing import Optional
from layers.memory import Conversation


class StateManager:
    """运行时对话状态的内存存储。"""

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
        """批量加载从持久化存储中恢复的对话。"""
        self._store.update(conversations)
