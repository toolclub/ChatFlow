"""
author: leizihao
email: lzh19162600626@gmail.com
"""
import httpx
import json
from typing import AsyncGenerator
from config import API_BASE_URL, API_KEY, CHAT_NUM_CTX, SUMMARY_NUM_CTX


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def chat_stream(
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
    num_ctx: int = CHAT_NUM_CTX,
) -> AsyncGenerator[str, None]:
    """
    流式调用 /v1/chat/completions（OpenAI 兼容格式），用于对话主模型。
    逐 chunk 返回生成的文本。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream(
            "POST",
            f"{API_BASE_URL}/chat/completions",
            json=payload,
            headers=_headers(),
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[len("data: "):]
                if raw.strip() == "[DONE]":
                    break
                data = json.loads(raw)
                delta = data["choices"][0]["delta"]
                content = delta.get("content")
                if content:
                    yield content


async def chat_sync(
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    num_ctx: int = SUMMARY_NUM_CTX,
) -> str:
    """
    非流式调用，用于摘要压缩模型等内部任务。
    直接返回完整文本。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_BASE_URL}/chat/completions",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def get_embedding(text: str, model: str) -> list[float]:
    """
    获取文本的 Embedding 向量（预留，后续 RAG 用）。
    """
    payload = {"model": model, "input": text}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_BASE_URL}/embeddings",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()
        return data["data"][0]["embedding"]


async def list_models() -> list[str]:
    """获取可用模型列表。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{API_BASE_URL}/models", headers=_headers())
        data = resp.json()
        return [m["id"] for m in data.get("models", data.get("data", []))]
