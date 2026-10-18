"""
LLM 供应商管理服务 — 进程内激活快照 + 跨 worker 失效

管理页激活/停用供应商后：
  1. 写 DB（db/llm_provider_store）
  2. publish Redis 失效消息（chatflow:llm_config_invalidate）→ 各 worker refresh_from_db()
  3. 每 15s 定时 refresh 兜底（pub/sub 丢消息时最终一致）

无激活配置（快照 None）时所有 resolve_* 回退 .env 常量 —— 行为与供应商管理上线前
完全一致；因此 Redis/DB 异常只降级（logger.warning），绝不阻断请求路径。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from config import (
    API_KEY,
    CHAT_MODEL,
    LLM_BASE_URL,
    ROUTE_MODEL_MAP,
    ROUTER_MODEL,
    SEARCH_MODEL,
    SUMMARY_MODEL,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
)
from db import llm_provider_store

logger = logging.getLogger("services.llm_provider_service")

# env 里的「角色模型名」集合：调用点传这些名字时想要的是某个角色（摘要/路由/搜索），
# 快照存在时应映射到快照对应角色的模型，而不是把 env 的名字透传给新供应商
# （新供应商大概率不认识 env 里那些本地模型名）。
_ENV_ROLE_MODELS: set[str] = set()
for _role_model in (SUMMARY_MODEL, ROUTER_MODEL, SEARCH_MODEL, *ROUTE_MODEL_MAP.values()):
    if _role_model:
        _ENV_ROLE_MODELS.add(_role_model)

# 定时刷新间隔（秒）：pub/sub 丢消息的兜底，也覆盖「Redis 挂了」的降级场景
_REFRESH_INTERVAL = 15.0


@dataclass
class ActiveProviderSnapshot:
    """当前激活供应商的解析结果（进程内只读副本，切换时整体替换，绝不原地改）。"""
    base_url: str
    api_key: str
    chat_model: str
    summary_model: str = ""       # 空 = 同 chat_model
    router_model: str = ""        # 空 = 同 chat_model
    vision_model: str = ""        # 空 = 沿用 .env VISION_*（视觉不随供应商切换）
    extra_models: list[str] = field(default_factory=list)


# 进程内快照（None = 无激活配置，走 .env 默认）
_snapshot: ActiveProviderSnapshot | None = None
_refresh_task: asyncio.Task | None = None


async def refresh_from_db() -> None:
    """从 DB 重读激活配置并整体替换快照（disabled/删除 → None，回退 env）。"""
    global _snapshot
    try:
        row = await llm_provider_store.get_active_provider()
    except Exception as exc:
        # DB 抖动时沿用当前快照，避免把正在服务的配置打回 env
        logger.warning("LLM 供应商快照刷新失败，沿用当前快照: %s", exc)
        return
    if row is None:
        if _snapshot is not None:
            logger.info("LLM 供应商激活配置已失效，回退 .env 默认")
        _snapshot = None
        return
    _snapshot = ActiveProviderSnapshot(
        base_url=row.base_url,
        api_key=row.api_key,
        chat_model=row.chat_model,
        summary_model=row.summary_model or "",
        router_model=row.router_model or "",
        vision_model=row.vision_model or "",
        extra_models=list(row.extra_models or []),
    )
    logger.info(
        "LLM 供应商快照已更新 | provider=%s | base_url=%s | chat_model=%s",
        row.name, row.base_url, row.chat_model,
    )


def get_snapshot() -> ActiveProviderSnapshot | None:
    """当前激活快照（同步，给 llm 工厂等热路径用；None=走 .env）。"""
    return _snapshot


def map_model_name(model: str | None) -> str:
    """把 env 角色模型名映射为当前激活供应商的对应模型名（公开工具）。

    None / CHAT_MODEL → 快照.chat_model；SUMMARY_MODEL → 快照.summary_model；
    ROUTER/SEARCH/ROUTE_MODEL_MAP 角色名 → 快照.router_model；未知名透传。
    无快照时原样返回（model or CHAT_MODEL）。
    route_node 选 answer_model/tool_model 时调用它，保证写进 state/ctx/SSE 的
    模型名与工厂实际调用的模型一致（展示 == 实际）。
    """
    if _snapshot is None:
        return model or CHAT_MODEL
    if model is None or model == CHAT_MODEL:
        return _snapshot.chat_model
    if model == SUMMARY_MODEL:
        return _snapshot.summary_model or _snapshot.chat_model
    if model in _ENV_ROLE_MODELS:
        return _snapshot.router_model or _snapshot.chat_model
    return model


def resolve_chat(model: str | None) -> tuple[str, str, str]:
    """
    解析主 LLM 调用三元组 (base_url, api_key, model)。

    模型名映射规则（快照存在时，见 map_model_name）：
      - None / CHAT_MODEL（env 默认名）      → 快照.chat_model
      - SUMMARY_MODEL                        → 快照.summary_model or chat_model
      - ROUTER/SEARCH/ROUTE_MODEL_MAP 角色名  → 快照.router_model or chat_model
      - 其他（用户显式选择的模型，如 extra_models 里的）→ 原样透传
    无快照 → (LLM_BASE_URL, API_KEY, model or CHAT_MODEL)，与历史行为一致。

    已知局限（名字映射的固有缺陷）：env 角色名与 CHAT_MODEL 同名时（如 .env 里
    SUMMARY_MODEL == ROUTER_MODEL == CHAT_MODEL == "deepseek-v4-pro"），按名字相等
    无法区分角色，最前面的 chat 分支先命中 → 全部映射到快照.chat_model，
    快照的 summary_model / router_model 被静默忽略。
    角色差异化靠角色感知函数 resolve_summary() / resolve_router()（不经名字映射），
    摘要与意图路由调用点必须用它们；本函数仍服务 answer_model / 用户透传等路径。
    """
    if _snapshot is None:
        return LLM_BASE_URL, API_KEY, model or CHAT_MODEL
    return _snapshot.base_url, _snapshot.api_key, map_model_name(model)


def resolve_summary() -> tuple[str, str, str]:
    """解析摘要角色三元组（角色感知：直接按角色取快照配置，不经名字映射）。

    快照存在 → (快照.base_url, 快照.api_key, 快照.summary_model or chat_model)；
    无快照 → (LLM_BASE_URL, API_KEY, SUMMARY_MODEL)。
    """
    if _snapshot:
        return (
            _snapshot.base_url,
            _snapshot.api_key,
            _snapshot.summary_model or _snapshot.chat_model,
        )
    return LLM_BASE_URL, API_KEY, SUMMARY_MODEL


def resolve_router() -> tuple[str, str, str]:
    """解析路由/意图分类角色三元组（角色感知，理由同 resolve_summary）。

    快照存在 → (快照.base_url, 快照.api_key, 快照.router_model or chat_model)；
    无快照 → (LLM_BASE_URL, API_KEY, ROUTER_MODEL)。
    """
    if _snapshot:
        return (
            _snapshot.base_url,
            _snapshot.api_key,
            _snapshot.router_model or _snapshot.chat_model,
        )
    return LLM_BASE_URL, API_KEY, ROUTER_MODEL


def resolve_default_request_model() -> str:
    """用户请求未显式指定模型时的默认模型名（快照优先，回退 CHAT_MODEL）。"""
    return _snapshot.chat_model if _snapshot else CHAT_MODEL


def resolve_vision() -> tuple[str, str, str]:
    """
    解析视觉模型三元组。

    快照存在且配置了 vision_model → 视觉也随供应商切换；
    否则沿用 .env VISION_*（model 回退链 VISION_MODEL > CHAT_MODEL）。
    """
    if _snapshot and _snapshot.vision_model:
        return _snapshot.base_url, _snapshot.api_key, _snapshot.vision_model
    return VISION_BASE_URL, VISION_API_KEY, VISION_MODEL or CHAT_MODEL


def visible_models() -> list[str]:
    """用户在聊天界面可见的模型列表（去重保序；无快照时由 model_router 走 env 逻辑）。"""
    if _snapshot is None:
        return []
    return list(dict.fromkeys([_snapshot.chat_model, *_snapshot.extra_models]))


async def start() -> None:
    """
    lifespan 启动（run_migrations 之后、graph init 之前）：
    初始快照 + Redis 失效订阅 + 定时兜底刷新。

    Redis/DB 不可用只 logger.warning，不阻断启动 —— 快照保持 None 即回退 .env。
    """
    global _refresh_task
    await refresh_from_db()
    try:
        from db.redis_state import start_llm_config_invalidation_listener
        await start_llm_config_invalidation_listener(refresh_from_db)
    except Exception as exc:
        logger.warning("LLM 配置失效订阅启动失败（依赖 %.0fs 定时刷新兜底）: %s",
                       _REFRESH_INTERVAL, exc)

    async def _periodic_refresh():
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            await refresh_from_db()

    _refresh_task = asyncio.create_task(_periodic_refresh())


async def stop() -> None:
    """lifespan 关闭：取消定时任务与 Redis 订阅。"""
    global _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        _refresh_task = None
    try:
        from db.redis_state import stop_llm_config_invalidation_listener
        await stop_llm_config_invalidation_listener()
    except Exception as exc:
        logger.warning("LLM 配置失效订阅停止异常（忽略）: %s", exc)
