"""量化模块 Redis 缓存

设计：
  - spec 铁律「永远不信任进程内数据」→ 跨 worker 共享走 Redis
  - key 设计带日期，自动跨日失效
  - 序列化用 pickle + base64：与现有 redis_state 的 decode_responses=True 兼容
  - 失败优雅降级：读失败/写失败都不抛错，让 caller 走 provider 兜底
"""
from __future__ import annotations

import base64
import logging
import pickle
from datetime import datetime

import pandas as pd

logger = logging.getLogger("quant.cache")

_SPOT_KEY_PREFIX = "chatflow:quant:spot:"
_SPOT_TTL_SECONDS = 600  # 10 分钟


def _spot_key(market: str) -> str:
    date = datetime.now().strftime("%Y%m%d")
    return f"{_SPOT_KEY_PREFIX}{market}:{date}"


def _serialize(df: pd.DataFrame) -> str:
    return base64.b64encode(pickle.dumps(df)).decode("ascii")


def _deserialize(payload: str) -> pd.DataFrame:
    return pickle.loads(base64.b64decode(payload.encode("ascii")))


async def get_spot_cached(market: str) -> pd.DataFrame | None:
    """读 spot 缓存。Redis 不可用 / 缓存缺失 / 反序列化失败时返回 None。"""
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        raw = await r.get(_spot_key(market))
        if not raw:
            return None
        df = _deserialize(raw)
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        return df
    except Exception as exc:
        logger.warning("读取 spot 缓存失败（降级走 provider）: %s", exc)
        return None


async def set_spot_cached(market: str, df: pd.DataFrame) -> None:
    """写 spot 缓存，TTL 10 分钟。失败仅 warn。"""
    if df is None or df.empty:
        return
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        key = _spot_key(market)
        payload = _serialize(df)
        await r.set(key, payload, ex=_SPOT_TTL_SECONDS)
        logger.info(
            "spot 缓存已写入 key=%s rows=%d ttl=%ds",
            key, len(df), _SPOT_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("写入 spot 缓存失败（不影响请求）: %s", exc)


async def clear_spot_cached(market: str = "cn_a") -> int:
    """清除指定市场所有 spot 缓存（含历史日期 key）。"""
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        pattern = f"{_SPOT_KEY_PREFIX}{market}:*"
        cursor: int | bytes = 0
        deleted = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                deleted += await r.delete(*keys)
            if int(cursor) == 0:
                break
        return deleted
    except Exception as exc:
        logger.warning("清理 spot 缓存失败: %s", exc)
        return 0
