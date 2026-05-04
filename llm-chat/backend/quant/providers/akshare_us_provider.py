"""AKShare US Provider — 美股数据源

使用 akshare.stock_us_spot_em 和 akshare.stock_us_hist。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pandas as pd

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
    Stock,
)
from quant.providers.symbols import to_internal

logger = logging.getLogger("quant.akshare_us")


_SPOT_RENAME = {
    "代码": "em_code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amount",
    "成交量": "volume",
    "成交额": "amount",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "prev_close",
    "换手率": "turnover_rate",
    "市盈率": "pe",
    "总市值": "market_cap",
}

_HIST_RENAME = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amount",
    "换手率": "turnover_rate",
}


class AKShareUSProvider:
    """AKShare 美股数据源。"""

    name = "akshare_us"
    capabilities = {
        ProviderCapability.STOCK_LIST,
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
    }

    def __init__(self, priority: int = 110, max_concurrency: int = 4) -> None:
        self.priority = priority
        self._sem = asyncio.Semaphore(max_concurrency)
        # 缓存 em_code 映射: ticker -> em_code (e.g. AAPL -> 105.AAPL)
        self._em_code_map: dict[str, str] = {}

    async def health_check(self) -> ProviderHealth:
        try:
            import akshare as ak
            await asyncio.to_thread(ak.stock_us_spot_em)
            return ProviderHealth(status=ProviderHealthStatus.OK)
        except Exception as exc:
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message=f"US Spot check failed: {exc}",
            )

    async def list_stocks(self, market: str = "us_stock") -> list[Stock]:
        df = await self.realtime_snapshot(market=market)
        if df.empty:
            return []
        return [
            Stock(symbol=row["symbol"], name=str(row.get("name", "")), market="us_stock")
            for _, row in df.iterrows()
        ]

    async def realtime_snapshot(self, market: str = "us_stock") -> pd.DataFrame:
        if market != "us_stock":
            return pd.DataFrame()
        
        df = await asyncio.to_thread(self._fetch_spot)
        return df

    def _fetch_spot(self) -> pd.DataFrame:
        import akshare as ak
        try:
            raw = ak.stock_us_spot_em()
        except Exception as exc:
            logger.error("Fetch US spot failed: %s", exc)
            return pd.DataFrame()

        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw.rename(columns={k: v for k, v in _SPOT_RENAME.items() if k in raw.columns})
        
        # 提取 ticker 并构建内部 symbol
        if "em_code" in df.columns:
            def _parse_ticker(em_code: str) -> str:
                # em_code usually like "105.AAPL"
                parts = em_code.split(".")
                ticker = parts[-1]
                self._em_code_map[ticker] = em_code
                return f"{ticker}.US"
            
            df["symbol"] = df["em_code"].apply(_parse_ticker)
        
        numeric_cols = ["price", "change_pct", "change_amount", "volume", "amount", "high", "low", "open", "prev_close", "turnover_rate", "pe", "market_cap"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        # 美股市值通常是美元，这里为了统一，后续可能需要汇率转换，目前先按原始值
        # EM 的总市值单位可能是“元”（人民币或美元视接口而定），通常美股接口返回的是美元
        
        df["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
        return df

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        # 确保有 em_code 映射，如果没有则拉一次 spot
        if not self._em_code_map:
            await self.realtime_snapshot()

        results = []
        for sym in symbols:
            ticker = sym.split(".")[0]
            em_code = self._em_code_map.get(ticker)
            if not em_code:
                # 尝试猜测: 默认 NASDAQ (105)
                em_code = f"105.{ticker}"
            
            async with self._sem:
                try:
                    df = await asyncio.to_thread(self._fetch_hist, em_code, start, end, adjust)
                    if not df.empty:
                        df["symbol"] = sym
                        results.append(df)
                except Exception as exc:
                    logger.warning("Fetch US hist failed for %s: %s", sym, exc)

        if not results:
            return pd.DataFrame()
        return pd.concat(results, ignore_index=True)

    @staticmethod
    def _fetch_hist(em_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
        import akshare as ak
        # ak.stock_us_hist start_date/end_date format: YYYYMMDD
        s = start.replace("-", "")
        e = end.replace("-", "")
        
        raw = ak.stock_us_hist(symbol=em_code, period="daily", start_date=s, end_date=e, adjust=adjust)
        if raw is None or raw.empty:
            return pd.DataFrame()
        
        df = raw.rename(columns={k: v for k, v in _HIST_RENAME.items() if k in raw.columns})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        
        numeric_cols = ["open", "close", "high", "low", "volume", "amount", "change_pct", "change_amount", "turnover_rate"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
