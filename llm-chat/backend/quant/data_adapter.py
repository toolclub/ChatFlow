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
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from quant import cache_disk
from quant.domain import ProviderCapability, ProviderTrace
from quant.provider_registry import ProviderRegistry, get_registry

logger = logging.getLogger("quant.data_adapter")


# ── 交易日新鲜度判定 ──────────────────────────────────────────────────────────
# 周末规避 + 收盘时间判定；不识别节假日（节假日时缓存会被判陈旧 → 触发回源 →
# provider 返回最近交易日数据 → 覆盖缓存，自动收敛）。
# 不调用 trading_calendar provider 是因为这是缓存判定的热路径，不能引入 IO。

_NY_TZ = ZoneInfo("America/New_York")
_CST_TZ = ZoneInfo("Asia/Shanghai")

# 收盘时间（含 30min 数据延迟缓冲，避免刚收盘就回源拉到当天空数据）
_US_CLOSE = time(16, 30)   # NYSE 收盘 16:00 EDT/EST，留 30min buffer
_CN_CLOSE = time(15, 30)   # 沪深收盘 15:00 CST，留 30min buffer


def _expected_last_trading_day(market: str, now: datetime | None = None) -> date:
    """根据当前时间和市场，推算"预期最新交易日"。

    判定规则：
      - 美股：以 NY 时间为准；当日是周一-周五且已过 16:30 EDT 收盘 → 当日；否则前一个工作日
      - A 股：以 CST 时间为准；当日是周一-周五且已过 15:30 CST 收盘 → 当日；否则前一个工作日

    返回的日期保证是工作日（Mon-Fri）；不识别节假日（缓存陈旧时会自动回源补救）。
    """
    if now is None:
        now = datetime.now(tz=ZoneInfo("UTC"))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))

    if market == "us_stock":
        local = now.astimezone(_NY_TZ)
        close_time = _US_CLOSE
    else:
        local = now.astimezone(_CST_TZ)
        close_time = _CN_CLOSE

    d = local.date()
    is_weekday = local.weekday() < 5  # 0=Mon ... 4=Fri
    after_close = local.time() >= close_time

    # 当天非工作日 / 未到收盘 → 退到前一日
    if not (is_weekday and after_close):
        d = d - timedelta(days=1)

    # 退到最近的工作日（处理周末）
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


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

        # ── 日期新鲜度判定（spec §"原则：永远不信任进程内数据"在缓存上的体现）──
        # 单看 cover_ratio 会让缓存永久卡在首次拉取那天的数据（详见 cache_warmer
        # 跑了几天但 K 线日期不更新的 bug）。必须叠加"最新日期 ≥ 预期交易日"。
        cached_max_date = _max_cached_date(cached)
        # readonly 不判预期：调用方明确"绝不回源"
        request_end_date = e_date if e_date else date.today()
        expected_last = _expected_last_trading_day(market) if not readonly else cached_max_date
        # 若用户请求的 end 早于预期最新日（请求历史区间），按 end 比对
        compare_target = min(expected_last, request_end_date) if expected_last else request_end_date
        is_fresh = (
            cached_max_date is not None
            and compare_target is not None
            and cached_max_date >= compare_target
        )

        if cover_ratio >= 0.8 and is_fresh:
            if trace is not None:
                trace.append(ProviderTrace(
                    provider="disk_cache",
                    capability=ProviderCapability.DAILY_BARS.value,
                    status="ok",
                    elapsed_ms=0.0,
                    rows=len(cached) if cached is not None else 0,
                    error=f"coverage={cover_ratio:.0%},max_date={cached_max_date}",
                ))
            return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True) if cached is not None else pd.DataFrame()

        # readonly 模式：缓存不足/陈旧也直接返回已有数据（绝不回源）
        if readonly:
            if cached is not None and not cached.empty:
                return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True)
            return pd.DataFrame()

        # 2) 回源策略：
        #    - 缓存日期陈旧（is_fresh=False）→ 全部标的都要拉最新日，不能只拉 missing
        #    - 缓存日期新鲜但有 missing 标的 → 只拉 missing
        cached_syms = set(cached["symbol"].astype(str).unique()) if cached is not None and not cached.empty else set()
        missing_syms = list(sym_set - cached_syms)

        if not is_fresh:
            # 不新鲜：无论 missing 多少，全量重拉到最新日（merge 时 drop_duplicates
            # keep=first 保留 cached 旧记录，new_df 只补足新增日期）。
            missing_syms = list(sym_set)
            logger.info(
                "bars 缓存日期陈旧 market=%s max_cached=%s expected=%s → 强制全量回源 (%d 只)",
                market, cached_max_date, compare_target, len(missing_syms),
            )
        elif not missing_syms:
            # 新鲜且全部命中 → 直接返回缓存
            return cached[cached["symbol"].astype(str).isin(sym_set)].reset_index(drop=True) if cached is not None else pd.DataFrame()
        else:
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

def _max_cached_date(df: pd.DataFrame | None) -> date | None:
    """从缓存 df 取最大的 date 列值；空 / 异常时返回 None。"""
    if df is None or df.empty or "date" not in df.columns:
        return None
    try:
        return pd.to_datetime(df["date"]).max().date()
    except Exception:
        return None


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
