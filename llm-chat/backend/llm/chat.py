"""
LLM 工厂：AsyncOpenAI 客户端实例管理

替代 langchain_openai.ChatOpenAI，使用原生 openai SDK。
使用模块级缓存（HTTP 客户端按 base_url 缓存，LLMClient 按 endpoint+credentials+model+temperature 缓存），
避免每次请求都新建 HTTP 连接。

供应商管理（cc-switch 模式）：工厂经 services/llm_provider_service 解析
(base_url, api_key, model) —— 管理页激活供应商后走快照配置，无激活时回退 .env。
切换供应商后缓存 key 自然变化（新 base/model → 新条目），旧条目留在缓存无害，
不做清空（避免并发请求踩到正在重建的客户端）。

对外接口：
    get_chat_llm(model, temperature)   → LLMClient    ── 主 LLM（名字映射）
    get_summary_llm()                  → LLMClient    ── 摘要专用（角色感知）
    get_router_llm(temperature)        → LLMClient    ── 路由/意图分类专用（角色感知）
    get_vision_llm(model, temperature) → LLMClient    ── 视觉 LLM
    get_vision_async_client()          → AsyncOpenAI  ── 视觉原始客户端（流式打点用）
"""
import json

from openai import AsyncOpenAI

from llm.client import LLMClient

from llm.providers import get_provider_for_model
from services import llm_provider_service

# ── 底层 HTTP 客户端缓存（按 base_url+api_key 复用） ──────────────────────────
_http_cache: dict[str, AsyncOpenAI] = {}

# ── LLMClient 封装缓存（按 base_url+api_key+model+temperature 缓存） ─────────────────
_client_cache: dict[tuple, LLMClient] = {}


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
    role: str = "chat",
) -> LLMClient:
    """通用工厂：按（base_url, api_key, model, temperature）缓存并返回 LLMClient。"""
    from config import settings
    options = settings.llm_role_options.get(role, {})
    cache_key = (base_url, api_key, model, temperature, role, json.dumps(options, sort_keys=True))
    if cache_key not in _client_cache:
        http_client = _get_http_client(base_url, api_key)

        # ── 适配提供商抽象（DI 注入） ─────────────────────────────────────────
        provider = get_provider_for_model(model, role=role)

        _client_cache[cache_key] = LLMClient(
            client=http_client,
            model=model,
            temperature=temperature,
            provider=provider,
            request_options=options,
        )
    return _client_cache[cache_key]


def get_chat_llm(model: str | None = None, temperature: float = 0.7) -> LLMClient:
    """
    返回主 LLM 实例（按 base_url+api_key+model+temperature 缓存）。

    model 允许 None（= 默认对话模型）。供应商快照存在时用快照的 base_url/api_key，
    并把 env 角色模型名（CHAT/SUMMARY/ROUTER/SEARCH/MAP）映射到快照对应角色；
    无快照时回退 .env（LLM_BASE_URL / API_KEY），行为与供应商管理上线前一致。
    适用于：
      - route_model、planner、call_model（非视觉路径）
      - call_model_after_tool（非视觉路径）
      - reflector
    """
    base_url, api_key, effective_model = llm_provider_service.resolve_chat(model)
    return _make_client(base_url, api_key, effective_model, temperature)


def get_summary_llm() -> LLMClient:
    """
    返回摘要专用 LLM 实例（低温度，输出更稳定）。

    角色感知（resolve_summary）：SUMMARY_MODEL 角色 + temperature=0.2。
    不走 resolve_chat 的名字映射 —— env 角色名与 CHAT_MODEL 同名时名字映射
    无法区分角色（会退化到 chat_model），角色感知直接取快照.summary_model。
    """
    base_url, api_key, effective_model = llm_provider_service.resolve_summary()
    return _make_client(base_url, api_key, effective_model, temperature=0.2, role="summary")


def get_router_llm(temperature: float = 0.0) -> LLMClient:
    """
    返回路由/意图分类专用 LLM 实例（temperature=0.0，输出确定性最高）。

    角色感知（resolve_router）：路由决策调用点（route_node）专用。
    与 get_summary_llm 同理，不经名字映射，不受 env 角色名与 CHAT_MODEL
    同名碰撞的影响，快照存在时直接取快照.router_model。
    """
    base_url, api_key, effective_model = llm_provider_service.resolve_router()
    return _make_client(base_url, api_key, effective_model, temperature, role="router")


def get_vision_llm(
    model: str | None = None,
    temperature: float = 0.1,
) -> LLMClient:
    """
    返回视觉模型 LLM 实例。

    优先级：
      1. 快照配置了 vision_model → 快照的 base_url/api_key/vision_model（显式 model 忽略）
      2. 否则沿用 .env VISION_BASE_URL / VISION_API_KEY，
         模型走旧回退链：显式传入 model（多模态会话选型）> VISION_MODEL > CHAT_MODEL
    适用于：vision_node、call_model/call_model_after_tool 的视觉路径。
    """
    snapshot = llm_provider_service.get_snapshot()
    base_url, api_key, effective_model = llm_provider_service.resolve_vision()
    if not (snapshot and snapshot.vision_model):
        # 无快照视觉覆盖：保留旧回退链（显式 model 优先）
        effective_model = model or effective_model
    return _make_client(base_url, api_key, effective_model, temperature, role="vision")


def get_vision_async_client() -> AsyncOpenAI:
    """
    视觉专用 AsyncOpenAI 原始客户端（vision_node / save_response_node 直连场景）。

    按 resolve_vision() 的 base_url+api_key 复用共享缓存，
    管理页切换供应商（配置了 vision_model）后自动换端点。
    """
    base_url, api_key, _ = llm_provider_service.resolve_vision()
    return _get_http_client(base_url, api_key)
