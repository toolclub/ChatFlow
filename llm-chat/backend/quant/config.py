"""量化模块配置（从全局 settings 读取，并提供模块级常量）"""
from __future__ import annotations

from config import settings


def _list_or_default(value, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


QUANT_ENABLED: bool = bool(getattr(settings, "quant_enabled", False))
QUANT_PROVIDER_ORDER: list[str] = _list_or_default(
    getattr(settings, "quant_provider_order", None),
    default=["akshare", "akshare_us"],
)
QUANT_BARS_CONCURRENCY: int = int(getattr(settings, "quant_bars_concurrency", 8))
QUANT_DEFAULT_TOP_N: int = int(getattr(settings, "quant_default_top_n", 30))
QUANT_FIRST_PASS_KEEP: int = int(getattr(settings, "quant_first_pass_keep", 50))
QUANT_HTTP_TIMEOUT: int = int(getattr(settings, "quant_http_timeout", 20))

# ── 磁盘缓存 / 预热 ──────────────────────────────────────────────────────────
QUANT_CACHE_DIR: str = str(getattr(settings, "quant_cache_dir", "./.quant_cache"))
QUANT_CACHE_RETENTION_DAYS: int = int(getattr(settings, "quant_cache_retention_days", 90))
QUANT_CACHE_MAX_SIZE_MB: int = int(getattr(settings, "quant_cache_max_size_mb", 200))
QUANT_WARMER_ENABLED: bool = bool(getattr(settings, "quant_warmer_enabled", True))
QUANT_WARMER_SPOT_INTERVAL: int = int(getattr(settings, "quant_warmer_spot_interval", 1800))
QUANT_WARMER_BARS_HOUR: int = int(getattr(settings, "quant_warmer_bars_hour", 4))
QUANT_WARMER_INDEX_HOUR: int = int(getattr(settings, "quant_warmer_index_hour", 7))
QUANT_SPOT_FRESH_SECONDS: int = int(getattr(settings, "quant_spot_fresh_seconds", 3600))
QUANT_FORCE_CACHE: bool = bool(getattr(settings, "quant_force_cache", True))
QUANT_BARS_LOOKBACK_DAYS: int = int(getattr(settings, "quant_bars_lookback_days", 120))
