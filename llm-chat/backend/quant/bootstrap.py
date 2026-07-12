"""启动期初始化 — 按 QUANT_PROVIDER_ORDER 注册 provider 并探活

只受 QUANT_ENABLED 约束，**不** 受 QUANT_SCREENING_ENABLED 约束：
股票预测通过 quant.data_adapter → provider_registry 拉 K 线，选股关掉后 provider
仍必须可用。选股开关只关「选股业务 + 缓存预热器」（见 quant/cache_warmer.py）。
"""
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

    if name == "yfinance_us":
        try:
            from quant.providers.yfinance_us_provider import YFinanceUSProvider
        except ImportError as exc:
            logger.warning("YFinance US provider 加载失败：%s", exc)
            return None
        return YFinanceUSProvider(
            priority=priority,
            max_concurrency=QUANT_BARS_CONCURRENCY,
        )

    if name == "akshare_us":
        try:
            from quant.providers.akshare_us_provider import AKShareUSProvider
        except ImportError as exc:
            logger.warning("AKShare US provider 加载失败：%s", exc)
            return None
        return AKShareUSProvider(
            priority=priority,
            max_concurrency=QUANT_BARS_CONCURRENCY,
        )

    if name == "twelvedata_us":
        try:
            from quant.providers.twelvedata_us_provider import TwelveDataUSProvider
        except ImportError as exc:
            logger.warning("TwelveData US provider 加载失败：%s", exc)
            return None
        return TwelveDataUSProvider(
            priority=priority,
            max_concurrency=QUANT_BARS_CONCURRENCY,
        )

    if name == "tiingo_us":
        try:
            from quant.providers.tiingo_us_provider import TiingoUSProvider
        except ImportError as exc:
            logger.warning("Tiingo US provider 加载失败：%s", exc)
            return None
        return TiingoUSProvider(
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
