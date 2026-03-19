"""
Layer 4 – Runtime
Agent loop: streaming generation and synchronous LLM calls.
"""
from typing import AsyncGenerator
from ollama_client import chat_stream as _stream, chat_sync as _sync


async def stream(
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """Streaming agent loop – yields text chunks for the conversation model."""
    async for chunk in _stream(model=model, messages=messages, temperature=temperature):
        yield chunk


async def call_sync(
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
) -> str:
    """Synchronous call – used for internal tasks such as summarisation."""
    return await _sync(model=model, messages=messages, temperature=temperature)
