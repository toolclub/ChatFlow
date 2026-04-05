"""
LLM 工厂：AsyncOpenAI 客户端实例管理

替代 langchain_openai.ChatOpenAI，使用原生 openai SDK。
使用模块级缓存（HTTP 客户端按 base_url 缓存，LLMClient 按 model+temperature 缓存），
避免每次请求都新建 HTTP 连接。

对外接口：
    get_chat_llm(model, temperature) → LLMClient   ── 主 LLM（LLM_BASE_URL）
    get_summary_llm()                → LLMClient   ── 摘要专用 LLM（低温度）
    get_vision_llm(model, temperature) → LLMClient ── 视觉 LLM（VISION_BASE_URL）
"""
from openai import AsyncOpenAI

from config import (
    API_KEY,
    CHAT_MODEL,
    LLM_BASE_URL,
    SUMMARY_MODEL,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
)
from llm.client import LLMClient

# ── 底层 HTTP 客户端缓存（按 base_url+api_key 复用） ──────────────────────────
_http_cache: dict[str, AsyncOpenAI] = {}

# ── LLMClient 封装缓存（按 base_url+model+temperature 缓存） ─────────────────
_client_cache: dict[str, LLMClient] = {}


def _get_http_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """获取或创建共享的底层 AsyncOpenAI HTTP 客户端。"""
    cache_key = f"{base_url}:{api_key}"
    if cache_key not in _http_cache:
        _http_cache[cache_key] = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return _http_cache[cache_key]


def _make_client(
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
) -> LLMClient:
    """通用工厂：按（base_url, model, temperature）缓存并返回 LLMClient。"""
    cache_key = f"{base_url}:{model}:{temperature}"
    if cache_key not in _client_cache:
        http_client = _get_http_client(base_url, api_key)
        _client_cache[cache_key] = LLMClient(
            client=http_client,
            model=model,
            temperature=temperature,
        )
    return _client_cache[cache_key]


def get_chat_llm(model: str = CHAT_MODEL, temperature: float = 0.7) -> LLMClient:
    """
    返回主 LLM 实例（按 model+temperature 缓存）。

    使用 LLM_BASE_URL / API_KEY，适用于：
      - route_model、planner、call_model（非视觉路径）
      - call_model_after_tool（非视觉路径）
      - reflector
    """
    return _make_client(LLM_BASE_URL, API_KEY, model, temperature)


def get_summary_llm() -> LLMClient:
    """
    返回摘要专用 LLM 实例（低温度，输出更稳定）。

    使用 SUMMARY_MODEL + temperature=0.2。
    """
    return _make_client(LLM_BASE_URL, API_KEY, SUMMARY_MODEL, temperature=0.2)


def get_vision_llm(
    model: str | None = None,
    temperature: float = 0.1,
) -> LLMClient:
    """
    返回视觉模型 LLM 实例。

    使用 VISION_BASE_URL / VISION_API_KEY（可指向本地 Ollama）。
    model 默认使用 config.VISION_MODEL，未配置时回退到 CHAT_MODEL。
    适用于：vision_node、call_model/call_model_after_tool 的视觉路径。
    """
    effective_model = model or VISION_MODEL or CHAT_MODEL
    return _make_client(VISION_BASE_URL, VISION_API_KEY, effective_model, temperature)
