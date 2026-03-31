"""
对话数据结构（与原 layers/memory.py 保持完全向后兼容）
"""
import time
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str           # "user" | "assistant" | "system"
    content: str
    timestamp: float = field(default_factory=time.time)
    id: int = 0         # DB 自增主键（0 表示尚未持久化）


@dataclass
class Conversation:
    id: str
    title: str = "新对话"
    system_prompt: str = ""
    messages: list[Message] = field(default_factory=list)  # 完整历史，永不删除
    mid_term_summary: str = ""                              # 语义记忆（中期摘要）
    mid_term_cursor: int = 0                                # messages[:cursor] 已完成摘要
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    client_id: str = ""                                     # 浏览器唯一标识（localStorage 生成）
