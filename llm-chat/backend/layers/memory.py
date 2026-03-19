"""
Layer 3 – Memory
Data structures for the three memory tiers:
  messages        – episodic memory (full history, never deleted)
  mid_term_summary– semantic memory (rolling summary of older messages)
  mid_term_cursor – index up to which messages have been summarised
  long_term       – reserved for RAG / vector retrieval
"""
import time
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str           # "user" | "assistant" | "system"
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Conversation:
    id: str
    title: str = "新对话"
    system_prompt: str = ""
    messages: list[Message] = field(default_factory=list)  # full history – never deleted
    mid_term_summary: str = ""                             # semantic memory
    mid_term_cursor: int = 0                               # messages[:cursor] already summarised
    # long_term_collection: str = ""                       # reserved: RAG collection name
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
