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
        *,
        readonly: bool = False,
        force: bool = False,
    ) -> pd.DataFrame:
        # 1) disk
        # force=True 时跳过缓存，直接回源（供 warmer 定期刷新用）
        if not force and (readonly or await cache_disk.is_spot_fresh(market)):
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

        # readonly 模式：即使磁盘不新鲜，如果上面没读到（可能文件损坏），也不回源
        if readonly:
            logger.warning("spot 模式为 readonly，但磁盘缓存为空或损坏，返回空数据")
            return pd.DataFrame()

        # 2) provider fallback
        async def invoker(provider):
            df = await provider.realtime_snapshot(market=market)
            return df, len(df)

        df = await self._registry.call_with_fallback(
            ProviderCapability.REALTIME_SNAPSHOT, invoker, trace,
            market=market,
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
        *,
        readonly: bool = False,
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        s_date = _to_date(start)
        e_date = _to_date(end)
        sym_set = set(symbols)

        cached: pd.DataFrame | None = None
        if len(symbols) == 1:
            cached = await cache_disk.read_bars_for_symbol(market, symbols[0], s_date, e_date)
        else:
            cached, _ = await cache_disk.read_bars_range(market, s_date, e_date)

        if cached is not None and not cached.empty:
            cover_ratio = len(
                set(cached["symbol"].astype(str).unique()) & sym_set
            ) / max(len(sym_set), 1) if "symbol" in cached.columns else 0.0
        else:
            cover_ratio = 0.0

        if cover_ratio >= 0.8:
            if trace is not None:
                trace.append(ProviderTrace(
                    provider="disk_cache",
                    capability=ProviderCapability.DAILY_BARS.value,
                    status="ok",
                    elapsed_ms=0.0,
                    rows=len(cached) if cached is not None else 0,
                    error=f"coverage={cover_ratio:.0%}",
                ))
            return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True) if cached is not None else pd.DataFrame()

        # readonly 模式：缓存不足直接返回已有数据（含空），绝不回源
        if readonly:
            if cached is not None and not cached.empty:
                return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
            return pd.DataFrame()

        # 2) 增量回源：仅请求缺失的标的
        cached_syms = set(cached["symbol"].astype(str).unique()) if cached is not None and not cached.empty else set()
        missing_syms = list(sym_set - cached_syms)
        
        if not missing_syms:
            return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True) if cached is not None else pd.DataFrame()

        logger.info("bars 缓存缺失: %d/%d, 仅请求缺失标的", len(missing_syms), len(sym_set))

        async def invoker(provider):
            df = await provider.daily_bars(
                missing_syms, start=_to_str(s_date), end=_to_str(e_date),
            )
            return df, len(df)

        try:
            new_df = await self._registry.call_with_fallback(
                ProviderCapability.DAILY_BARS, invoker, trace,
                market=market,
            )
            # 写盘（新拉取的部分）
            if new_df is not None and not new_df.empty:
                try:
                    await cache_disk.write_bars(market, new_df)
                except Exception as exc:
                    logger.warning("bars 写盘失败: %s", exc)
                
                # 合并缓存与新抓取的数据
                if cached is not None and not cached.empty:
                    df = pd.concat([cached, new_df], ignore_index=True).drop_duplicates(["symbol", "date"])
                else:
                    df = new_df
                return df[df["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
        except Exception as exc:
            logger.warning("daily_bars 增量回源失败：%s", exc)
        
        # 失败则返回已有的部分
        if cached is not None and not cached.empty:
            return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
        return pd.DataFrame()

    # ── index 成分 ──────────────────────────────────────────────────────────

    async def index_constituents(
        self,
        index_code: str,
        trace: list[ProviderTrace] | None = None,
        market: str | None = None,
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
            market=market,
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
