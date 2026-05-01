"""启动期初始化 — 按 QUANT_PROVIDER_ORDER 注册 provider 并探活"""
from __future__ import annotations

import logging

from config import settings
from quant.config import (
    QUANT_BARS_CONCURRENCY,
    QUANT_ENABLED,
    QUANT_PROVIDER_ORDER,
)
from quant.provider_registry import get_registry

logger = logging.getLogger("quant.bootstrap")


async def init_quant() -> None:
    if not QUANT_ENABLED:
        logger.info("量化模块已禁用（QUANT_ENABLED=false）")
        return

    registry = get_registry()
    for idx, name in enumerate(QUANT_PROVIDER_ORDER):
        priority = idx * 10 + 10
        provider = _build_provider(name, priority=priority)
        if provider is None:
            continue
        registry.register(provider)

    if not registry.list_providers():
        logger.warning(
            "量化模块启用但无可用 provider — 检查 QUANT_PROVIDER_ORDER 与依赖/配置",
        )
        return

    try:
        await registry.refresh_health()
    except Exception as exc:
        logger.error("provider 健康检查失败: %s", exc)


def _build_provider(name: str, priority: int):
    name = name.strip().lower()
    if not name:
        return None

    if name == "akshare":
        try:
            from quant.providers.akshare_provider import AKShareProvider
        except ImportError as exc:
            logger.warning("AKShare provider 加载失败：%s", exc)
            return None
        return AKShareProvider(
            priority=priority,
            max_concurrency=QUANT_BARS_CONCURRENCY,
        )

    if name == "baostock":
        if not getattr(settings, "baostock_enabled", True):
            logger.info("BaoStock 已禁用（BAOSTOCK_ENABLED=false），跳过注册")
            return None
        try:
            from quant.providers.baostock_provider import BaoStockProvider
        except ImportError as exc:
            logger.warning("BaoStock provider 加载失败（缺 baostock 包?）: %s", exc)
            return None
        return BaoStockProvider(priority=priority)

    if name == "tushare":
        token = getattr(settings, "tushare_token", "") or ""
        if not token:
            logger.info("TUSHARE_TOKEN 未配置，跳过 Tushare provider 注册")
            return None
        try:
            from quant.providers.tushare_provider import TushareProvider
        except ImportError as exc:
            logger.warning("Tushare provider 加载失败（缺 tushare 包?）: %s", exc)
            return None
        return TushareProvider(
            token=token,
            priority=priority,
            max_concurrency=QUANT_BARS_CONCURRENCY,
        )

    logger.warning("未知 quant provider：%s（已跳过）", name)
    return None
