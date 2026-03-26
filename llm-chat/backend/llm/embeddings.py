"""
Embedding 工厂：OllamaEmbeddings 单例
Embedding 模型本地运行，使用 Ollama 原生 API（/api/embed），稳定可靠。
如果将来要用 OpenAI Embeddings，把 OLLAMA_BASE_URL 换成 LLM_BASE_URL 并改用 OpenAIEmbeddings 即可。
"""
from langchain_ollama import OllamaEmbeddings
from config import EMBEDDING_MODEL, OLLAMA_BASE_URL

_instance: OllamaEmbeddings | None = None


def get_embeddings() -> OllamaEmbeddings:
    """返回 OllamaEmbeddings 单例。"""
    global _instance
    if _instance is None:
        _instance = OllamaEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=OLLAMA_BASE_URL,
        )
    return _instance


async def embed_text(text: str) -> list[float]:
    """对单条文本做向量化，返回 float 列表。"""
    return await get_embeddings().aembed_query(text)
