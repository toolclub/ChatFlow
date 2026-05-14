"""
Redis 跨 worker 共享状态 — 替代进程内 dict

解决的问题：
  - _stop_events: 进程内 dict → Redis pub/sub
  - _active_sessions: 进程内 dict → Redis key 带 TTL
  - _store 缓存失效: 进程内 dict → Redis pub/sub 通知其他 worker 失效

所有 worker 共享 Redis 实例（已在 docker-compose 中部署）。
"""
import asyncio
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

from config import REDIS_URL

logger = logging.getLogger("db.redis_state")

# ── Redis 客户端（复用语义缓存的 Redis 实例） ──────────────────────────────────
_redis: Optional[aioredis.Redis] = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL 未配置，Redis 共享状态不可用")
        _redis = aioredis.from_url(
            REDIS_URL, decode_responses=True,
            max_connections=20, socket_keepalive=True, socket_connect_timeout=5,
        )
    return _redis


# ══════════════════════════════════════════════════════════════════════════════
# 1. 停止信号（替代 _stop_events 进程内 dict）
# ══════════════════════════════════════════════════════════════════════════════

_STOP_KEY_PREFIX = "chatflow:stop:"
_STOP_TTL = 60  # 60 秒自动过期（防止残留）


async def publish_stop(conv_id: str) -> None:
    """发布停止信号（任意 worker 都能收到）。"""
    r = _get_redis()
    key = f"{_STOP_KEY_PREFIX}{conv_id}"
    await r.set(key, "1", ex=_STOP_TTL)
    logger.info("发布停止信号 | conv=%s", conv_id)


async def check_stop(conv_id: str) -> bool:
    """检查是否有停止信号。"""
    r = _get_redis()
    key = f"{_STOP_KEY_PREFIX}{conv_id}"
    val = await r.get(key)
    return val == "1"


async def clear_stop(conv_id: str) -> None:
    """清除停止信号。"""
    r = _get_redis()
    await r.delete(f"{_STOP_KEY_PREFIX}{conv_id}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. 活跃会话注册（替代 _active_sessions 进程内 dict）
# ══════════════════════════════════════════════════════════════════════════════

_STREAMING_KEY_PREFIX = "chatflow:streaming:"
_STREAMING_TTL = 600  # 10 分钟 TTL（心跳续期，超时自动清理）


async def register_streaming(conv_id: str, worker_id: str = "") -> None:
    """注册当前对话为活跃流式会话（跨 worker 可见）。"""
    r = _get_redis()
    key = f"{_STREAMING_KEY_PREFIX}{conv_id}"
    await r.set(key, worker_id or str(time.time()), ex=_STREAMING_TTL)


async def heartbeat_streaming(conv_id: str) -> None:
    """续期活跃会话 TTL（心跳时调用）。"""
    r = _get_redis()
    key = f"{_STREAMING_KEY_PREFIX}{conv_id}"
    await r.expire(key, _STREAMING_TTL)


async def unregister_streaming(conv_id: str) -> None:
    """注销活跃会话。"""
    r = _get_redis()
    await r.delete(f"{_STREAMING_KEY_PREFIX}{conv_id}")


async def is_streaming(conv_id: str) -> bool:
    """检查对话是否正在流式生成（跨 worker）。"""
    r = _get_redis()
    key = f"{_STREAMING_KEY_PREFIX}{conv_id}"
    return await r.exists(key) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. 缓存失效通知（替代 _store 本地缓存的跨 worker 失效）
# ══════════════════════════════════════════════════════════════════════════════

_INVALIDATION_CHANNEL = "chatflow:cache_invalidate"

# 本地订阅任务（在 app 启动时初始化）
_sub_task: Optional[asyncio.Task] = None


async def publish_cache_invalidation(conv_id: str) -> None:
    """通知所有 worker 失效指定对话的本地缓存。"""
    r = _get_redis()
    await r.publish(_INVALIDATION_CHANNEL, conv_id)


async def start_cache_invalidation_listener(on_invalidate) -> None:
    """
    启动缓存失效监听（在 lifespan 中调用）。

    on_invalidate: async callable(conv_id: str) — 收到失效通知后的回调。
    """
    global _sub_task

    async def _listen():
        r = _get_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(_INVALIDATION_CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    conv_id = msg["data"]
                    if conv_id:
                        await on_invalidate(conv_id)
        except asyncio.CancelledError:
            await pubsub.unsubscribe(_INVALIDATION_CHANNEL)
        except Exception as exc:
            logger.warning("缓存失效监听异常: %s", exc)

    _sub_task = asyncio.create_task(_listen())
    logger.info("缓存失效监听已启动")


async def stop_cache_invalidation_listener() -> None:
    """停止缓存失效监听。"""
    global _sub_task
    if _sub_task:
        _sub_task.cancel()
        _sub_task = None


# ══════════════════════════════════════════════════════════════════════════════
# 4. 网页搜索预算 + 去重（防止模型反复触发 web_search）
# ══════════════════════════════════════════════════════════════════════════════

_WEBSEARCH_COUNT_PREFIX = "chatflow:websearch:count:"
_WEBSEARCH_CACHE_PREFIX = "chatflow:websearch:cache:"
_WEBSEARCH_TTL = 1800  # 30 分钟，覆盖单轮对话生命周期


async def incr_websearch_count(scope: str) -> int:
    """递增本 scope（一般为 assistant_message_id）的搜索计数，返回新值。"""
    r = _get_redis()
    key = f"{_WEBSEARCH_COUNT_PREFIX}{scope}"
    new_val = await r.incr(key)
    if new_val == 1:
        await r.expire(key, _WEBSEARCH_TTL)
    return int(new_val)


async def get_websearch_cache(scope: str, query: str) -> Optional[str]:
    """读取本 scope 下同 query 的上次结果（JSON 字符串）。"""
    r = _get_redis()
    key = f"{_WEBSEARCH_CACHE_PREFIX}{scope}"
    val = await r.hget(key, query)
    return val if val else None


async def set_websearch_cache(scope: str, query: str, value: str) -> None:
    """写入本 scope 下 query 的结果，供下次同 query 复用。"""
    r = _get_redis()
    key = f"{_WEBSEARCH_CACHE_PREFIX}{scope}"
    await r.hset(key, query, value)
    await r.expire(key, _WEBSEARCH_TTL)
