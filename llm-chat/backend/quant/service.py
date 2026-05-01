"""选股服务 — 编排 universe → 硬过滤 → 一轮缩减 → 取行情 → 因子 → 排序"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta

import pandas as pd

from quant import cache as quant_cache
from quant.config import QUANT_DEFAULT_TOP_N, QUANT_FIRST_PASS_KEEP
from quant.data_adapter import CachedDataAdapter, get_adapter
from quant.domain import (
    FactorScore,
    ProviderCapability,
    ProviderTrace,
    ScreenCriteria,
    ScreenResult,
)
from quant.factors import (
    compose_scores,
    compute_fundamental_factors,
    compute_liquidity_factors,
    compute_risk_factors,
    compute_technical_factors,
    zscore,
)
from quant.factors.scorer import to_score_100
from quant.provider_registry import ProviderRegistry, get_registry

logger = logging.getLogger("quant.timer")


class QuantScreeningService:
    """选股编排 — 通过 CachedDataAdapter 拿数据（disk → registry fallback）。"""

    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        adapter: CachedDataAdapter | None = None,
    ) -> None:
        self._registry = registry or get_registry()
        # adapter 默认绑定 self._registry — 测试传 registry 时不会拿到全局 adapter
        if adapter is not None:
            self._adapter = adapter
        elif registry is not None:
            self._adapter = CachedDataAdapter(registry=registry)
        else:
            self._adapter = get_adapter()

    async def screen(self, criteria: ScreenCriteria, snapshot_id: str | None = None) -> ScreenResult:
        t_total_start = time.perf_counter()
        logger.info("🚀 [选股] 开始流程 | snapshot_id=%s", snapshot_id)
        criteria = self._normalize_criteria(criteria)
        provider_trace: list[ProviderTrace] = []
        warnings: list[str] = []

        # 1. 实时行情
        t0 = time.perf_counter()
        spot = await self._fetch_spot(provider_trace)
        if spot.empty:
            logger.warning("    ❌ 快照数据为空，流程终止")
            warnings.append("快照数据为空")
            return self._empty_result(criteria, provider_trace, warnings)
        logger.info("    ✅ 阶段 1: 获取实时行情完成 | 数量: %d | 耗时: %.0fms", len(spot), (time.perf_counter() - t0) * 1000)

        # 2. 股票池加载
        t1 = time.perf_counter()
        spot, universe_size = await self._load_universe(spot, criteria, provider_trace, warnings)
        if spot.empty:
            logger.warning("    ❌ 股票池为空，流程终止")
            warnings.append("股票池为空")
            return self._empty_result(criteria, provider_trace, warnings, universe_size)
        logger.info("    ✅ 阶段 2: 加载股票池完成 | 池大小: %d | 耗时: %.0fms", len(spot), (time.perf_counter() - t1) * 1000)

        # 3. 硬过滤
        t2 = time.perf_counter()
        spot = self._apply_hard_filters(spot, criteria, warnings)
        if spot.empty:
            logger.warning("    ❌ 硬过滤后无候选标的")
            warnings.append("筛选条件过严，无候选标的")
            return self._empty_result(criteria, provider_trace, warnings, universe_size)
        logger.info("    ✅ 阶段 3: 硬过滤完成 | 剩余候选: %d | 耗时: %.0fms", len(spot), (time.perf_counter() - t2) * 1000)

        # 4. 初筛缩减
        t3 = time.perf_counter()
        first_keep = max(criteria.top_n * 4, QUANT_FIRST_PASS_KEEP)
        narrowed = self._first_pass_narrow(spot, first_keep)
        candidates: list[str] = narrowed["symbol"].tolist()
        logger.info("    ✅ 阶段 4: 初筛缩减完成 | 目标计算量: %d | 耗时: %.0fms", len(candidates), (time.perf_counter() - t3) * 1000)

        # 5. 历史 K 线
        t4 = time.perf_counter()
        bars = await self._fetch_bars(candidates, criteria, provider_trace, warnings)
        logger.info("    ✅ 阶段 5: 获取历史 K 线完成 | 数据行数: %d | 耗时: %.0fms", len(bars), (time.perf_counter() - t4) * 1000)

        # 6. 因子计算
        t5 = time.perf_counter()
        tech_df = compute_technical_factors(
            narrowed, bars,
            momentum_window=criteria.momentum_window,
            volatility_window=criteria.volatility_window,
        )
        fund_df = compute_fundamental_factors(narrowed, bars=bars)
        liq_df = compute_liquidity_factors(narrowed, bars)
        risk_df = compute_risk_factors(narrowed)

        idx = pd.Index(candidates, name="symbol").drop_duplicates()
        tech_df = tech_df.reindex(idx)
        fund_df = fund_df.reindex(idx)
        liq_df = liq_df.reindex(idx)
        risk_df = risk_df.reindex(idx).fillna(False)

        tech_z, fund_z, liq_z, risk_z = self._score_categories(
            tech_df, fund_df, liq_df, risk_df,
        )

        total_z = compose_scores(
            {
                "technical": tech_z,
                "fundamental": fund_z,
                "liquidity": liq_z,
                "risk": risk_z,
            },
            weights=criteria.weights,
        )
        logger.info("    ✅ 阶段 6: 因子与打分计算完成 | 耗时: %.0fms", (time.perf_counter() - t5) * 1000)

        # 7. 组装结果
        t6 = time.perf_counter()
        rows = self._build_rows(
            narrowed=narrowed,
            tech=tech_df, fund=fund_df, liq=liq_df, risk=risk_df,
            tech_z=tech_z, fund_z=fund_z, liq_z=liq_z, risk_z=risk_z,
            total_z=total_z,
        )
        rows.sort(key=lambda r: r.total, reverse=True)
        rows = rows[: criteria.top_n]
        for i, r in enumerate(rows, start=1):
            r.rank = i

        elapsed_ms = (time.perf_counter() - t_total_start) * 1000
        logger.info(
            "🏁 [选股] 流程结束 | universe=%d narrowed=%d top_n=%d | 总耗时=%.1fms",
            universe_size, len(narrowed), len(rows), elapsed_ms,
        )

        return ScreenResult(
            snapshot_id=snapshot_id or f"qs_{uuid.uuid4().hex[:12]}",
            criteria=criteria,
            rows=rows,
            provider_trace=provider_trace,
            weights=criteria.weights,
            universe_size=universe_size,
            as_of_date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=time.time(),
            warnings=warnings,
        )

    async def get_stock_chart_data(self, symbol: str, days: int = 240) -> dict:
        """获取个股 K 线图数据（适配 ECharts）。"""
        end_d = datetime.now().date()
        # 考虑到非交易日，days * 1.6 确保覆盖足够的自然日
        start_d = end_d - timedelta(days=int(days * 1.6))

        df = await self._adapter.bars(
            symbols=[symbol],
            start=start_d,
            end=end_d,
            market="cn_a",
        )
        if df.empty:
            return {"dates": [], "values": [], "volumes": [], "ma5": [], "ma10": [], "ma20": []}

        df = df.sort_values("date").reset_index(drop=True)
        
        # 计算均线
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()

        # ECharts Candlestick 格式：[open, close, low, high]
        values = df[["open", "close", "low", "high"]].values.tolist()
        dates = df["date"].astype(str).tolist()
        volumes = []
        for i, row in df.iterrows():
            # [index, volume, color_flag]
            color_flag = 1 if row["close"] >= row["open"] else -1
            volumes.append([i, row["volume"], color_flag])

        return {
            "symbol": symbol,
            "dates": dates,
            "values": values,
            "volumes": volumes,
            "ma5": df["ma5"].replace({float('nan'): None}).tolist(),
            "ma10": df["ma10"].replace({float('nan'): None}).tolist(),
            "ma20": df["ma20"].replace({float('nan'): None}).tolist(),
        }

    # ── 各阶段实现 ─────────────────────────────────────────────────────────

    def _normalize_criteria(self, c: ScreenCriteria) -> ScreenCriteria:
        c = c.model_copy(deep=True)
        for k in ("technical", "fundamental", "liquidity", "risk"):
            c.weights.setdefault(k, 0.0)
        if c.top_n <= 0:
            c.top_n = QUANT_DEFAULT_TOP_N
        if c.momentum_window <= 0:
            c.momentum_window = 60
        if c.volatility_window < 5:
            c.volatility_window = 20
        return c

    async def _fetch_spot(self, trace: list[ProviderTrace]) -> pd.DataFrame:
        """读 spot：disk_cache → Redis（COMPAT）→ provider fallback。

        adapter 已实现 disk_cache + provider 链；这里多保留一层 Redis 兜底，
        便于跨 worker 的 10 分钟级共享（disk 由 warmer 周期刷，Redis 由请求顺手刷）。
        """
        df = await self._adapter.spot("cn_a", trace)
        if df is not None and not df.empty:
            # 顺手填 Redis（10min TTL），保证 spot 至少有两层缓存
            try:
                await quant_cache.set_spot_cached("cn_a", df)
            except Exception:
                pass
            return df

        # adapter 未拿到（极端情况），尝试 Redis
        cached = await quant_cache.get_spot_cached("cn_a")
        if cached is not None and not cached.empty:
            trace.append(ProviderTrace(
                provider="redis_cache",
                capability=ProviderCapability.REALTIME_SNAPSHOT.value,
                status="fallback",
                elapsed_ms=0.0,
                rows=len(cached),
            ))
            return cached
        return pd.DataFrame()

    async def _load_universe(
        self,
        spot: pd.DataFrame,
        criteria: ScreenCriteria,
        trace: list[ProviderTrace],
        warnings: list[str],
    ) -> tuple[pd.DataFrame, int]:
        universe_size = len(spot)
        if criteria.universe == "all":
            return spot, universe_size

        if criteria.universe == "custom":
            symbols = set(criteria.custom_symbols)
            if not symbols:
                warnings.append("custom 模式但未提供 custom_symbols，回退到 all")
                return spot, universe_size
            keep = spot[spot["symbol"].isin(symbols)].reset_index(drop=True)
            return keep, len(keep)

        try:
            symbols = await self._adapter.index_constituents(criteria.universe, trace)
        except Exception as exc:
            logger.warning("获取指数 %s 成分股失败，回退全市场: %s", criteria.universe, exc)
            warnings.append(f"获取指数成分股失败：{exc}; 回退到全市场")
            return spot, universe_size

        if not symbols:
            warnings.append(f"指数 {criteria.universe} 成分股为空，回退到全市场")
            return spot, universe_size
        sym_set = set(symbols)
        keep = spot[spot["symbol"].isin(sym_set)].reset_index(drop=True)
        return keep, len(keep)

    def _apply_hard_filters(
        self,
        spot: pd.DataFrame,
        c: ScreenCriteria,
        warnings: list[str],
    ) -> pd.DataFrame:
        df = spot.copy()
        before = len(df)

        if c.exclude_st and "name" in df.columns:
            name_up = df["name"].astype(str).str.upper()
            mask_st = (
                name_up.str.startswith("ST")
                | name_up.str.startswith("*ST")
                | name_up.str.startswith("S*ST")
                | df["name"].astype(str).str.contains("退", na=False)
            )
            df = df[~mask_st]

        if c.exclude_suspended and "volume" in df.columns:
            vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            df = df[vol > 0]

        df = self._apply_numeric_filter(
            df, "market_cap", c.min_market_cap, "ge",
            label="min_market_cap", warnings=warnings,
        )
        df = self._apply_numeric_filter(
            df, "amount", c.min_avg_turnover, "ge",
            label="min_avg_turnover", warnings=warnings,
        )
        df = self._apply_range_filter(
            df, "pe", c.pe_range, label="pe_range", warnings=warnings,
        )
        df = self._apply_range_filter(
            df, "pb", c.pb_range, label="pb_range", warnings=warnings,
        )

        warnings.append(f"硬过滤：{before} → {len(df)}")
        return df.reset_index(drop=True)

    @staticmethod
    def _apply_numeric_filter(
        df: pd.DataFrame, col: str, threshold,
        op: str, label: str, warnings: list[str],
    ) -> pd.DataFrame:
        if threshold is None or col not in df.columns:
            return df
        s = pd.to_numeric(df[col], errors="coerce")
        nan_ratio = s.isna().sum() / max(len(s), 1)
        if nan_ratio > 0.5:
            warnings.append(
                f"{label}: 数据缺失率 {nan_ratio:.0%}，过滤已跳过"
            )
            return df
        mask = s >= threshold if op == "ge" else s <= threshold
        return df[mask]

    @staticmethod
    def _apply_range_filter(
        df: pd.DataFrame, col: str, rng,
        label: str, warnings: list[str],
    ) -> pd.DataFrame:
        if rng is None or col not in df.columns:
            return df
        lo, hi = rng
        s = pd.to_numeric(df[col], errors="coerce")
        nan_ratio = s.isna().sum() / max(len(s), 1)
        if nan_ratio > 0.5:
            warnings.append(
                f"{label}: 数据缺失率 {nan_ratio:.0%}，过滤已跳过"
            )
            return df
        return df[s.between(lo, hi)]

    def _first_pass_narrow(self, spot: pd.DataFrame, keep: int) -> pd.DataFrame:
        if len(spot) <= keep:
            return spot.reset_index(drop=True)
        if "amount" in spot.columns:
            return spot.sort_values("amount", ascending=False).head(keep).reset_index(drop=True)
        return spot.head(keep).reset_index(drop=True)

    async def _fetch_bars(
        self,
        symbols: list[str],
        c: ScreenCriteria,
        trace: list[ProviderTrace],
        warnings: list[str],
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        days_needed = max(c.momentum_window, c.volatility_window) + 10
        end_d = datetime.now().date()
        start_d = end_d - timedelta(days=int(days_needed * 1.6))
        try:
            return await self._adapter.bars(
                symbols=symbols,
                start=start_d,
                end=end_d,
                market="cn_a",
                trace=trace,
            )
        except Exception as exc:
            logger.warning("日线拉取失败，技术因子退到 spot 兜底: %s", exc)
            warnings.append(f"日线拉取失败：{exc}; 技术因子退到 spot 兜底")
            return pd.DataFrame()

    # ── 评分 / 装配 ───────────────────────────────────────────────────────

    def _score_categories(
        self,
        tech: pd.DataFrame,
        fund: pd.DataFrame,
        liq: pd.DataFrame,
        risk: pd.DataFrame,
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        # 技术面：动量(+) - 波动率(-) - |均线偏离|(-)
        m_z = zscore(tech.get("momentum", pd.Series(dtype=float)))
        v_z = zscore(tech.get("volatility", pd.Series(dtype=float)))
        d_z = zscore(tech.get("ma_deviation", pd.Series(dtype=float)).abs())
        tech_z = (
            m_z.fillna(0) * 0.5
            - v_z.fillna(0) * 0.3
            - d_z.fillna(0) * 0.2
        )

        # 基本面：PE/PB 越低越好（仅正值参与，负值在 risk 里扣分）
        pe = pd.to_numeric(fund.get("pe", pd.Series(dtype=float)), errors="coerce")
        pb = pd.to_numeric(fund.get("pb", pd.Series(dtype=float)), errors="coerce")
        pe_pos = pe.where(pe > 0)
        pb_pos = pb.where(pb > 0)
        pe_z = (-zscore(pe_pos)).fillna(0)
        pb_z = (-zscore(pb_pos)).fillna(0)
        fund_z = pe_z * 0.5 + pb_z * 0.5

        # 流动性：成交额越高越好
        liq_z = zscore(liq.get("avg_turnover", pd.Series(dtype=float))).fillna(0)

        # 风险：标志命中扣分（越负越差）
        risk_z = pd.Series(0.0, index=risk.index)
        if "is_st" in risk.columns:
            risk_z = risk_z - risk["is_st"].astype(float) * 3.0
        if "is_suspended" in risk.columns:
            risk_z = risk_z - risk["is_suspended"].astype(float) * 5.0
        if "has_negative_pe" in risk.columns:
            risk_z = risk_z - risk["has_negative_pe"].astype(float) * 1.0

        return tech_z, fund_z, liq_z, risk_z

    def _build_rows(
        self,
        narrowed: pd.DataFrame,
        tech: pd.DataFrame,
        fund: pd.DataFrame,
        liq: pd.DataFrame,
        risk: pd.DataFrame,
        tech_z: pd.Series,
        fund_z: pd.Series,
        liq_z: pd.Series,
        risk_z: pd.Series,
        total_z: pd.Series,
    ) -> list[FactorScore]:
        tech_100 = to_score_100(tech_z)
        fund_100 = to_score_100(fund_z)
        liq_100 = to_score_100(liq_z)
        risk_100 = to_score_100(risk_z, base=70.0, scale=10.0)
        total_100 = to_score_100(total_z)

        narrowed_idx = narrowed.set_index("symbol")
        rows: list[FactorScore] = []
        for sym in narrowed["symbol"]:
            name = ""
            if sym in narrowed_idx.index:
                name_v = narrowed_idx.loc[sym].get("name", "")
                name = "" if pd.isna(name_v) else str(name_v)

            raw: dict = {}
            if sym in tech.index:
                t_row = tech.loc[sym]
                for k in ("momentum", "volatility", "ma_deviation"):
                    raw[k] = _to_float_or_none(t_row.get(k))
            if sym in fund.index:
                f_row = fund.loc[sym]
                for k in ("pe", "pb", "market_cap"):
                    raw[k] = _to_float_or_none(f_row.get(k))
            if sym in liq.index:
                raw["avg_turnover"] = _to_float_or_none(liq.loc[sym].get("avg_turnover"))

            warnings: list[str] = []
            if sym in risk.index:
                rr = risk.loc[sym]
                if bool(rr.get("is_st")):
                    warnings.append("ST 标的")
                if bool(rr.get("is_suspended")):
                    warnings.append("当日疑似停牌（成交量为 0）")
                if bool(rr.get("has_negative_pe")):
                    warnings.append("PE 为负，估值因子未参与打分")

            rows.append(FactorScore(
                symbol=sym,
                name=name,
                technical=round(float(tech_100.get(sym, 0.0)), 2),
                fundamental=round(float(fund_100.get(sym, 0.0)), 2),
                liquidity=round(float(liq_100.get(sym, 0.0)), 2),
                risk=round(float(risk_100.get(sym, 70.0)), 2),
                total=round(float(total_100.get(sym, 0.0)), 2),
                reasons=_build_reasons(raw),
                warnings=warnings,
                raw=raw,
            ))
        return rows

    def _empty_result(
        self,
        criteria: ScreenCriteria,
        trace: list[ProviderTrace],
        warnings: list[str],
        universe_size: int = 0,
    ) -> ScreenResult:
        return ScreenResult(
            snapshot_id=f"qs_{uuid.uuid4().hex[:12]}",
            criteria=criteria,
            rows=[],
            provider_trace=trace,
            weights=criteria.weights,
            universe_size=universe_size,
            as_of_date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=time.time(),
            warnings=warnings,
        )


# ── helpers ─────────────────────────────────────────────────────────────────

def _to_float_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_reasons(raw: dict) -> list[str]:
    out = []
    m = raw.get("momentum")
    if m is not None:
        out.append(f"动量 {m:+.2f}%")
    v = raw.get("volatility")
    if v is not None:
        out.append(f"波动率 {v:.2f}%")
    pe = raw.get("pe")
    if pe is not None and pe > 0:
        out.append(f"PE {pe:.2f}")
    pb = raw.get("pb")
    if pb is not None and pb > 0:
        out.append(f"PB {pb:.2f}")
    cap = raw.get("market_cap")
    if cap is not None:
        out.append(f"市值 {cap:.0f}亿")
    liq = raw.get("avg_turnover")
    if liq is not None:
        out.append(f"成交额 {liq:.2f}亿")
    return out


# ── 全局单例 ───────────────────────────────────────────────────────────────

_service: QuantScreeningService | None = None


def get_service() -> QuantScreeningService:
    global _service
    if _service is None:
        _service = QuantScreeningService()
    return _service


def reset_service() -> None:
    global _service
    _service = None
