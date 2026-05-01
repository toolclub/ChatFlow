"""Provider 注册表 — 按能力 + 优先级 + 健康度选择 provider"""
from __future__ import annotations

import logging
import time
import asyncio
import random

from quant.domain import (
    ProviderCapability,
    ProviderHealthStatus,
    ProviderInfo,
    ProviderTrace,
)
from quant.providers.base import MarketDataProvider

logger = logging.getLogger("quant.registry")


def _is_permanent_error(exc: BaseException) -> bool:
    """不应重试的错误：权限不足 / 积分不够 / 网络不通 / 明确空数据。"""
    msg = str(exc)
    if "权限" in msg or "积分" in msg or "没有接口" in msg:
        return True
    if "返回空 bars" in msg or "返回空 spot" in msg:
        return True
    # 网络不通 / 超时 → 不重试（第一次不通后面也不会通）
    if "网络接收错误" in msg or "登录失败" in msg:
        return True
    if "timeout" in msg.lower() or "超时" in msg:
        return True
    if "Connection" in msg and ("refused" in msg or "reset" in msg):
        return True
    return False


class NoProviderAvailable(RuntimeError):
    """所有 provider 都不可用或没有目标能力"""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: list[MarketDataProvider] = []
        self._health: dict[str, ProviderHealthStatus] = {}
        self._health_msg: dict[str, str] = {}

    def register(self, provider: MarketDataProvider) -> None:
        self._providers = [p for p in self._providers if p.name != provider.name]
        self._providers.append(provider)
        self._providers.sort(key=lambda p: p.priority)
        self._health.setdefault(provider.name, ProviderHealthStatus.OK)
        self._health_msg.setdefault(provider.name, "")
        logger.info("注册 provider: %s (priority=%d)", provider.name, provider.priority)

    def get_provider(self, name: str) -> MarketDataProvider | None:
        return next((p for p in self._providers if p.name == name), None)

    async def refresh_health(self) -> None:
        async def _check(p: MarketDataProvider):
            # 引入随机延迟，错开多个 worker 同时 import
            await asyncio.sleep(random.uniform(0.1, 2.0))
            try:
                h = await p.health_check()
                self._health[p.name] = h.status
                self._health_msg[p.name] = h.message
                logger.info("provider %s 健康: %s %s", p.name, h.status, h.message)
            except Exception as exc:
                self._health[p.name] = ProviderHealthStatus.DOWN
                self._health_msg[p.name] = f"health_check 异常：{type(exc).__name__}: {exc}"[:200]

        if not self._providers:
            return
            
        # 并行执行所有 provider 的健康检查
        await asyncio.gather(*[_check(p) for p in self._providers])

    def list_providers(self) -> list[ProviderInfo]:
        return [
            ProviderInfo(
                name=p.name,
                enabled=True,
                priority=p.priority,
                capabilities=sorted(c.value for c in p.capabilities),
                health=self._health.get(p.name, ProviderHealthStatus.OK),
                message=self._health_msg.get(p.name, ""),
            )
            for p in self._providers
        ]

    def candidates(self, capability: ProviderCapability) -> list[MarketDataProvider]:
        return [
            p for p in self._providers
            if capability in p.capabilities
            and self._health.get(p.name, ProviderHealthStatus.OK) != ProviderHealthStatus.DOWN
        ]

    async def call_with_fallback(
        self,
        capability: ProviderCapability,
        invoker,
        trace: list[ProviderTrace] | None = None,
        *,
        max_retries: int = 2,
    ):
        """按优先级依次尝试 provider 调用 invoker(provider)，遇错自动 fallback。

        每个 provider 在放弃前重试最多 max_retries 次（指数退避 + 随机抖动），
        权限类错误（积分/权限）不重试，立即跳到下一个 provider。

        invoker: async (provider) -> (result, rows_count)
        """
        import random as _random

        cs = self.candidates(capability)
        if not cs:
            raise NoProviderAvailable(f"无可用 provider 提供能力：{capability.value}")

        last_exc: Exception | None = None
        for idx, p in enumerate(cs):
            logger.info(
                "调用 provider %s (capability=%s, %d/%d)",
                p.name, capability.value, idx + 1, len(cs),
            )

            for attempt in range(max_retries + 1):
                t0 = time.perf_counter()
                try:
                    result, rows = await invoker(p)
                    if trace is not None:
                        trace.append(ProviderTrace(
                            provider=p.name,
                            capability=capability.value,
                            status="ok" if idx == 0 else "fallback",
                            elapsed_ms=(time.perf_counter() - t0) * 1000,
                            rows=int(rows or 0),
                        ))
                    return result
                except Exception as exc:
                    last_exc = exc
                    if _is_permanent_error(exc):
                        # 权限/积分/空数据 → 不重试，直接跳到下一个 provider
                        if trace is not None:
                            trace.append(ProviderTrace(
                                provider=p.name,
                                capability=capability.value,
                                status="error",
                                elapsed_ms=(time.perf_counter() - t0) * 1000,
                                error=f"{type(exc).__name__}: {exc}"[:200],
                            ))
                        logger.warning("provider %s 调用失败 (不可重试)，尝试 fallback：%s",
                                       p.name, exc)
                        break

                    if attempt < max_retries:
                        delay = (2 ** attempt) + _random.uniform(0.5, 1.5)
                        logger.warning(
                            "provider %s 调用失败 (第 %d/%d 次重试, %.1fs 后): %s",
                            p.name, attempt + 1, max_retries, delay, exc,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # 最后一次重试也失败
                    if trace is not None:
                        trace.append(ProviderTrace(
                            provider=p.name,
                            capability=capability.value,
                            status="error",
                            elapsed_ms=(time.perf_counter() - t0) * 1000,
                            error=f"{type(exc).__name__}: {exc}"[:200],
                        ))
                    logger.warning("provider %s 调用失败 (%d 次重试耗尽)，尝试 fallback：%s",
                                   p.name, max_retries, exc)
                    break

        raise NoProviderAvailable(
            f"所有 provider 都失败 (capability={capability.value}): {last_exc}"
        )


# ── 全局单例 ────────────────────────────────────────────────────────────────

_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
