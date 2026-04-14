"""
模型路由 — LLM 模型列表、Embedding 测试
"""
import httpx
from fastapi import APIRouter

from config import API_BASE_URL, CHAT_MODEL, EMBEDDING_MODEL

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
async def get_models():
    """列出 Ollama 中已下载的所有模型。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{API_BASE_URL}/api/tags")
            data = resp.json()
            models = [
                m["name"] for m in data.get("models", [])
                if not m["name"].startswith(EMBEDDING_MODEL.split(":")[0])
            ]
    except Exception:
        models = [CHAT_MODEL]
    return {"models": models}


@router.post("/embedding")
async def test_embedding(text: str = "测试文本"):
    from llm.embeddings import embed_text
    vec = await embed_text(text)
    return {
        "model": EMBEDDING_MODEL,
        "text": text,
        "dimensions": len(vec),
        "vector_preview": vec[:5],
    }
