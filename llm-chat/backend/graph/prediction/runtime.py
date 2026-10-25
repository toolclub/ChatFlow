"""Ownership leases for prediction jobs shared by all API workers."""
from __future__ import annotations

import asyncio
import uuid

LEASE_SECONDS = 30
_PREFIX = "chatflow:prediction:owner:"


async def acquire(prediction_id: str) -> str | None:
    from db.redis_state import _get_redis
    token = uuid.uuid4().hex
    ok = await asyncio.wait_for(
        _get_redis().set(_PREFIX + prediction_id, token, nx=True, ex=LEASE_SECONDS),
        timeout=2,
    )
    return token if ok else None


async def owned(prediction_id: str) -> bool:
    from db.redis_state import _get_redis
    return bool(await asyncio.wait_for(_get_redis().exists(_PREFIX + prediction_id), timeout=2))


async def request_cancel(prediction_id: str, reason: str) -> bool:
    from db.redis_state import _get_redis
    redis = _get_redis()
    token = await asyncio.wait_for(redis.get(_PREFIX + prediction_id), timeout=2)
    if not token:
        return False
    if isinstance(token, bytes):
        token = token.decode()
    await asyncio.wait_for(redis.set(
        _PREFIX + prediction_id + ":cancel", f"{token}:{reason}", ex=LEASE_SECONDS,
    ), timeout=2)
    return True


async def cancellation(prediction_id: str, token: str) -> str | None:
    from db.redis_state import _get_redis
    value = await asyncio.wait_for(
        _get_redis().get(_PREFIX + prediction_id + ":cancel"), timeout=2,
    )
    if isinstance(value, bytes):
        value = value.decode()
    if value and value.startswith(token + ":"):
        return value[len(token) + 1:]
    return None


async def renew(prediction_id: str, token: str) -> bool:
    from db.redis_state import _get_redis
    return bool(await asyncio.wait_for(_get_redis().eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
        1, _PREFIX + prediction_id, token, LEASE_SECONDS,
    ), timeout=2))


async def release(prediction_id: str, token: str) -> bool:
    from db.redis_state import _get_redis
    return bool(await asyncio.wait_for(_get_redis().eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1, _PREFIX + prediction_id, token,
    ), timeout=2))
