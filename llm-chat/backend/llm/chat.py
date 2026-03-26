"""
LLM 工厂：ChatOpenAI 实例管理（兼容任何 OpenAI 接口）
使用模块级缓存，避免每次请求都新建实例。
"""
from langchain_openai import ChatOpenAI
from config import CHAT_MODEL, SUMMARY_MODEL, LLM_BASE_URL, API_KEY

_cache: dict[str, ChatOpenAI] = {}


def get_chat_llm(model: str = CHAT_MODEL, temperature: float = 0.7) -> ChatOpenAI:
    """返回对话模型实例（按 model+temperature 缓存）。"""
    key = f"{model}:{temperature}"
    if key not in _cache:
        _cache[key] = ChatOpenAI(
            model=model,
            base_url=LLM_BASE_URL,
            api_key=API_KEY,
            temperature=temperature,
        )
    return _cache[key]


def get_summary_llm() -> ChatOpenAI:
    """返回摘要专用模型实例（低温度，结果更稳定）。"""
    key = f"{SUMMARY_MODEL}:0.2"
    if key not in _cache:
        _cache[key] = ChatOpenAI(
            model=SUMMARY_MODEL,
            base_url=LLM_BASE_URL,
            api_key=API_KEY,
            temperature=0.2,
        )
    return _cache[key]
