"""量化模块配置（从全局 settings 读取，并提供模块级常量）"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings

logger = logging.getLogger("quant.config")

# llm-chat/.env — 本地开发（python main.py）时 .env 不进 os.environ，
# 而 Settings 的 extra="ignore" 又会丢弃未在 config.Settings 里声明的键，
# 所以未声明的 QUANT_* 开关需要在这里兜底直接读一次 .env 文件。
# Docker 场景 docker-compose 的 env_file 已把 .env 注入 os.environ，走第一条分支。
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _raw_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is not None:
        return value
    try:
        if not _ENV_FILE.exists():
            return None
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == name:
                return val.strip().strip('"').strip("'")
    except OSError as exc:
        logger.warning("读取 .env 失败（%s 回退默认值）: %s", name, exc)
    return None


def _bool_env(env_name: str, settings_key: str, default: bool) -> bool:
    raw = _raw_env(env_name)
    if raw is None:
        value = getattr(settings, settings_key, None)
        return default if value is None else bool(value)
    return raw.strip().lower() in _TRUE_VALUES


def _list_or_default(value, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


QUANT_ENABLED: bool = bool(getattr(settings, "quant_enabled", False))

# ── 智能选股总开关 ────────────────────────────────────────────────────────────
# false 时关闭「选股业务 + 缓存预热器」，一个外部行情请求都不发（预热器是雅虎财经
# HTTP 429 限流的元凶，300 个 ticker 会打爆额度，导致股票预测拉不到 K 线）。
# 注意：它 **不** 关闭 provider 注册 / data_adapter / 行情数据访问 —— 股票预测仍
# 依赖 quant.data_adapter 拉 K 线，/api/quant/stock/{symbol}/chart 也照常可用。
QUANT_SCREENING_ENABLED: bool = _bool_env(
    "QUANT_SCREENING_ENABLED", "quant_screening_enabled", default=False
)
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
# 分布式部署时只让一个容器跑预热：QUANT_WARMER_ROLE=primary 才启动；secondary/off 直接跳过。
# 同机多 Worker 由 fcntl 文件锁兜底，跨机由这个环境变量决定唯一执行者。
QUANT_WARMER_ROLE: str = str(getattr(settings, "quant_warmer_role", "primary")).lower()
QUANT_WARMER_SPOT_INTERVAL: int = int(getattr(settings, "quant_warmer_spot_interval", 14400))
QUANT_WARMER_BARS_HOUR: int = int(getattr(settings, "quant_warmer_bars_hour", 4))
QUANT_WARMER_INDEX_HOUR: int = int(getattr(settings, "quant_warmer_index_hour", 7))
# tushare bulk-by-date 的并发线程数（pro.daily 限频通常 500/min，8 线程足够安全）
QUANT_TUSHARE_BULK_WORKERS: int = int(getattr(settings, "quant_tushare_bulk_workers", 8))
QUANT_SPOT_FRESH_SECONDS: int = int(getattr(settings, "quant_spot_fresh_seconds", 3600))
QUANT_FORCE_CACHE: bool = bool(getattr(settings, "quant_force_cache", True))
QUANT_BARS_LOOKBACK_DAYS: int = int(getattr(settings, "quant_bars_lookback_days", 120))

# yfinance_us universe 大小（按成交额降序取 top N，覆盖 K 线预热范围）
QUANT_YFINANCE_US_UNIVERSE_SIZE: int = int(getattr(settings, "quant_yfinance_us_universe_size", 300))
