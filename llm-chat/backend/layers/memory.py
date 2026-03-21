"""
第 3 层 – Memory（记忆）
三级记忆体系的数据结构：
  messages        – 情节记忆（完整历史，永不删除）
  mid_term_summary– 语义记忆（旧消息的滚动摘要）
  mid_term_cursor – 已完成摘要的消息截止索引
  long_term       – 预留，供 RAG / 向量检索使用
author: leizihao
email: lzh19162600626@gmail.com
"""
import time
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str           # "user"（用户）| "assistant"（助手）| "system"（系统）
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Conversation:
    id: str
    title: str = "新对话"
    system_prompt: str = ""
    messages: list[Message] = field(default_factory=list)  # 完整历史——永不删除
    mid_term_summary: str = ""                             # 语义记忆
    mid_term_cursor: int = 0                               # messages[:cursor] 已完成摘要
    # long_term_collection: str = ""                       # 预留：RAG 集合名称
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
