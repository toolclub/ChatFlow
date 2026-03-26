"""
Embedding 工厂：OpenAIEmbeddings 单例（兼容任何 OpenAI 接口）
"""
from langchain_openai import OpenAIEmbeddings
from config import EMBEDDING_MODEL, LLM_BASE_URL, API_KEY

_instance: OpenAIEmbeddings | None = None


def get_embeddings() -> OpenAIEmbeddings:
    """返回 OpenAIEmbeddings 单例。"""
    global _instance
    if _instance is None:
        _instance = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=LLM_BASE_URL,
            api_key=API_KEY,
        )
    return _instance


async def embed_text(text: str) -> list[float]:
    """对单条文本做向量化，返回 float 列表。"""
    return await get_embeddings().aembed_query(text)
