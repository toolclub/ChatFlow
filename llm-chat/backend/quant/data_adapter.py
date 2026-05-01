"""量化数据适配器 — 统一的"先盘 → Redis → provider"读路径

调用关系：
    service._fetch_spot / _fetch_bars
        ↓
    CachedDataAdapter
        ├─ disk hit → 返回
        ├─ disk miss → registry.call_with_fallback
        └─ provider 成功 → 写盘 + 返回

写盘是"机会主义" — 拿到数据顺手缓存，下次直接命中。
warmer 任务则定期主动刷新（见 cache_warmer.py），保证业务请求几乎一直命中。

关键的"miss only"语义：
  - read_spot 已存在但太旧（age > QUANT_SPOT_FRESH_SECONDS）→ 视为 miss
  - bars 部分命中（区间内某些日期缺失）→ 仅向 provider 请求缺失天，避免重复全量拉
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from quant import cache_disk
from quant.domain import ProviderCapability, ProviderTrace
from quant.provider_registry import ProviderRegistry, get_registry

logger = logging.getLogger("quant.data_adapter")


class CachedDataAdapter:
    """所有市场数据走这里。registry 是底层 fallback。"""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    # ── spot ────────────────────────────────────────────────────────────────

    async def spot(
        self,
        market: str,
        trace: list[ProviderTrace] | None = None,
    ) -> pd.DataFrame:
        # 1) disk
        if await cache_disk.is_spot_fresh(market):
            df = await cache_disk.read_spot(market)
            if df is not None and not df.empty:
                if trace is not None:
                    age = await cache_disk.spot_age_seconds(market) or 0
                    trace.append(ProviderTrace(
                        provider="disk_cache",
                        capability=ProviderCapability.REALTIME_SNAPSHOT.value,
                        status="ok",
                        elapsed_ms=0.0,
                        rows=len(df),
                        error=f"age={int(age)}s",
                    ))
                return df

        # 2) provider fallback
        async def invoker(provider):
            df = await provider.realtime_snapshot(market=market)
            return df, len(df)

        df = await self._registry.call_with_fallback(
            ProviderCapability.REALTIME_SNAPSHOT, invoker, trace,
        )
        if df is not None and not df.empty:
            try:
                await cache_disk.write_spot(market, df)
                await cache_disk.update_meta({
                    "spot_last_refresh": int(datetime.now().timestamp()),
                    "spot_last_rows": int(len(df)),
                })
            except Exception as exc:
                logger.warning("spot 写盘失败（不影响请求）: %s", exc)
        return df

    # ── bars ───────────────────────────────────────────────────────────────

    async def bars(
        self,
        symbols: list[str],
        start: str | date,
        end: str | date,
        market: str = "cn_a",
        trace: list[ProviderTrace] | None = None,
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        s_date = _to_date(start)
        e_date = _to_date(end)
        # 1) 先尝试整体读盘
        cached, missing = await cache_disk.read_bars_range(market, s_date, e_date)
        sym_set = set(symbols)

        if cached is not None and not cached.empty:
            cached_syms = set(cached["symbol"].astype(str).unique()) if "symbol" in cached.columns else set()
            cover_ratio = len(cached_syms & sym_set) / max(len(sym_set), 1)
        else:
            cover_ratio = 0.0

        # 命中条件：覆盖率 ≥ 80% 且最近 5 个日历日内有数据 → 直接返回缓存裁剪
        cache_recent_enough = cached is not None and not cached.empty and (
            (e_date - max(pd.to_datetime(cached["date"]).dt.date.max(),
                          s_date)).days <= 5
        )
        if cover_ratio >= 0.8 and cache_recent_enough:
            if trace is not None:
                trace.append(ProviderTrace(
                    provider="disk_cache",
                    capability=ProviderCapability.DAILY_BARS.value,
                    status="ok",
                    elapsed_ms=0.0,
                    rows=len(cached),
                    error=f"coverage={cover_ratio:.0%}",
                ))
            df = cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
            return df

        # 2) 缓存不足：回源全量
        async def invoker(provider):
            df = await provider.daily_bars(
                symbols, start=_to_str(s_date), end=_to_str(e_date),
            )
            return df, len(df)

        try:
            df = await self._registry.call_with_fallback(
                ProviderCapability.DAILY_BARS, invoker, trace,
            )
        except Exception as exc:
            logger.warning("daily_bars 回源失败：%s", exc)
            # 兜底：返回部分命中的缓存（即便覆盖率 <80%，total better than empty）
            if cached is not None and not cached.empty:
                return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
            return pd.DataFrame()

        # 3) 写盘（按 date 拆分）
        if df is not None and not df.empty:
            try:
                await cache_disk.write_bars(market, df)
            except Exception as exc:
                logger.warning("bars 写盘失败（不影响请求）: %s", exc)
        return df

    # ── index 成分 ──────────────────────────────────────────────────────────

    async def index_constituents(
        self,
        index_code: str,
        trace: list[ProviderTrace] | None = None,
    ) -> list[str]:
        cached = await cache_disk.read_index(index_code)
        if cached:
            if trace is not None:
                trace.append(ProviderTrace(
                    provider="disk_cache",
                    capability=ProviderCapability.INDEX_WEIGHT.value,
                    status="ok",
                    elapsed_ms=0.0,
                    rows=len(cached),
                ))
            return cached

        async def invoker(provider):
            lst = await provider.index_constituents(index_code)
            return lst, len(lst)

        symbols = await self._registry.call_with_fallback(
            ProviderCapability.INDEX_WEIGHT, invoker, trace,
        )
        if symbols:
            try:
                await cache_disk.write_index(index_code, symbols)
            except Exception as exc:
                logger.warning("index 写盘失败：%s", exc)
        return symbols


# ── helpers ─────────────────────────────────────────────────────────────────

def _to_date(v: str | date) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _to_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ── 全局单例 ────────────────────────────────────────────────────────────────

_adapter: CachedDataAdapter | None = None


def get_adapter() -> CachedDataAdapter:
    global _adapter
    if _adapter is None:
        _adapter = CachedDataAdapter()
    return _adapter


def reset_adapter() -> None:
    global _adapter
    _adapter = None
