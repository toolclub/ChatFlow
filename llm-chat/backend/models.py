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
    force_plan: Optional[list[dict]] = None  # 用户编辑后的强制计划，跳过 planner LLM 规划


class CreateConversationRequest(BaseModel):
    title: Optional[str] = "新对话"
    system_prompt: Optional[str] = ""


class UpdateConversationRequest(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
