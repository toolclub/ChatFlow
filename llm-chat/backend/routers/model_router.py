"""
模型路由 — LLM 模型列表、Embedding 测试
"""
import httpx
from fastapi import APIRouter

from config import API_BASE_URL, CHAT_MODEL, EMBEDDING_MODEL
from services import llm_provider_service

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
async def get_models():
    """列出用户可选的对话模型。

    管理页激活供应商时返回 [chat_model] + extra_models（去重保序）；
    无激活配置时维持原有行为（拉 Ollama /api/tags，失败回退 [CHAT_MODEL]）。
    铁律 #2：本接口绝不返回 base_url/api_key（那是 admin 专属信息）。
    """
    snapshot = llm_provider_service.get_snapshot()
    if snapshot:
        return {"models": llm_provider_service.visible_models()}

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
    from llm.embeddings import embed_text, EmbeddingError
    try:
        vec = await embed_text(text)
        return {
            "model": EMBEDDING_MODEL,
            "text": text,
            "dimensions": len(vec),
            "vector_preview": vec[:5],
        }
    except EmbeddingError as e:
        return {
            "error": str(e),
            "code": "EMBEDDING_FAILED",
        }
