"""
Embedding 工具：直接用 httpx 调用 OpenAI-compatible /v1/embeddings 接口。

为何不用 langchain_openai.OpenAIEmbeddings：
  langchain-openai >= 0.3 默认以 encoding_format=base64 请求，Ollama 不支持该格式，
  返回 400 "invalid input type"。直接 httpx 调用可精确控制请求参数。

兼容矩阵（只要有 /v1/embeddings 接口均可用）：
  Ollama   → EMBEDDING_BASE_URL=http://localhost:11434/v1
  OpenAI   → EMBEDDING_BASE_URL=https://api.openai.com/v1
  GLM      → EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
  MiniMax  → EMBEDDING_BASE_URL=https://api.minimaxi.com/v1
  Gitee AI → EMBEDDING_BASE_URL=https://ai.gitee.com/v1  （需配置 EMBEDDING_API_KEY）
"""
import logging as _logging

import httpx

from config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL

_client: httpx.AsyncClient | None = None
_embed_logger = _logging.getLogger("llm.embeddings")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # 远程云端 API（Gitee/OpenAI 等）首次请求可能较慢，给 20s；
        # Ollama 本地连接失败会立即报 ConnectError，不会真的等满超时。
        _client = httpx.AsyncClient(timeout=20.0)
    return _client


async def embed_text(text: str) -> list[float]:
    """对单条文本做向量化，返回 float 列表。

    支持独立 EMBEDDING_API_KEY（如 Gitee AI），未配置时回退到主 API_KEY。
    若 embedding 服务不可达（未启动、网络超时等），抛出异常由调用方捕获降级处理。
    """
    url = EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
    try:
        resp = await _get_client().post(
            url,
            json={"model": EMBEDDING_MODEL, "input": text, "encoding_format": "float"},
            headers={
                "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                "X-Failover-Enabled": "true",   # Gitee 模力方舟故障自动切换
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except httpx.ConnectError as e:
        _embed_logger.warning(
            "Embedding 服务连接失败（可能未启动）: %s  url=%s", e, url
        )
        raise
    except httpx.TimeoutException as e:
        _embed_logger.warning(
            "Embedding 服务超时（20s）: %s  url=%s", e, url
        )
        raise
    except httpx.HTTPStatusError as e:
        _embed_logger.warning(
            "Embedding 服务返回错误 %s: %s  url=%s",
            e.response.status_code, e.response.text[:200], url,
        )
        raise
