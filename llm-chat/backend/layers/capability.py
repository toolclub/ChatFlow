"""
Layer 2 – Capability
Tools available to the agent: model listing, embeddings (RAG-ready).
"""
from ollama_client import list_models as _list_models, get_embedding as _get_embedding


async def list_models() -> list[str]:
    return await _list_models()


async def get_embedding(text: str, model: str) -> list[float]:
    return await _get_embedding(text, model)
