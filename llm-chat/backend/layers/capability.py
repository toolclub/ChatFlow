"""
第 2 层 – Capability（能力）
代理可调用的工具：模型列表查询、嵌入向量生成（支持 RAG）。
author: leizihao
email: lzh19162600626@gmail.com
"""
from ollama_client import list_models as _list_models, get_embedding as _get_embedding


async def list_models() -> list[str]:
    return await _list_models()


async def get_embedding(text: str, model: str) -> list[float]:
    return await _get_embedding(text, model)
