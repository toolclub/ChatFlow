"""
第 4 层 – Runtime（运行时）
代理循环：流式生成与同步 LLM 调用。
author: leizihao
email: lzh19162600626@gmail.com
"""
from typing import AsyncGenerator
from ollama_client import chat_stream as _stream, chat_sync as _sync


async def stream(
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """流式代理循环——为对话模型逐块产出文本。"""
    async for chunk in _stream(model=model, messages=messages, temperature=temperature):
        yield chunk


async def call_sync(
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
) -> str:
    """同步调用——用于摘要生成等内部任务。"""
    return await _sync(model=model, messages=messages, temperature=temperature)
