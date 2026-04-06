"""
author: leizihao
email: lzh19162600626@gmail.com
"""
from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    model: Optional[str] = None       # 不传则用默认对话模型
    temperature: float = 0.7
    images: list[str] = []            # base64 编码的图片列表（已去除 data: 前缀）
    agent_mode: bool = True           # True=完整 Agent 图；False=简单直接对话图


class CreateConversationRequest(BaseModel):
    title: Optional[str] = "新对话"
    system_prompt: Optional[str] = ""


class UpdateConversationRequest(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
