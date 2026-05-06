"""YFinance US Provider — 美股数据源（海外直连，稳定）

通过 Yahoo Finance API 获取美股行情和 K 线。需要 VPN。
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

logger = logging.getLogger("quant.yfinance_us")

_SPOT_RENAME = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "price",
    "Volume": "volume",
}


class YFinanceUSProvider:
    """YFinance 美股数据源 — 作为 akshare_us 的主要 provider。"""

    name = "yfinance_us"
    capabilities = {
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
    }
    supported_markets = {"us_stock"}

    def __init__(self, priority: int = 20, max_concurrency: int = 8) -> None:
        self.priority = priority
        self._sem = asyncio.Semaphore(max_concurrency)

    async def health_check(self) -> ProviderHealth:
        try:
            import yfinance as yf

            df = await asyncio.to_thread(
                lambda: yf.download("AAPL", period="1d", auto_adjust=False)
            )
            if df is not None and not df.empty:
                return ProviderHealth(status=ProviderHealthStatus.OK)
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message="AAPL 1d 数据返回空",
            )
        except Exception as exc:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message=f"YFinance 不可用: {exc}",
            )

    # ── realtime_snapshot ─────────────────────────────────────────────────

    async def realtime_snapshot(self, market: str = "us_stock") -> pd.DataFrame:
        import yfinance as yf

        tickers = self._get_universe_tickers()
        if not tickers:
            return pd.DataFrame()

        df = await asyncio.to_thread(self._download_spot, tickers, yf)
        if df is None or df.empty:
            return pd.DataFrame()

        df["market"] = "us_stock"
        df["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
        return df

    @staticmethod
    def _download_spot(tickers: list[str], yf) -> pd.DataFrame:
        """分 3 批下载最近 5 日行情，计算涨跌幅等指标。"""
        frames: list[pd.DataFrame] = []
        batch_size = 200

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            try:
                raw = yf.download(
                    batch, period="5d", group_by="ticker",
                    auto_adjust=False, threads=4,
                )
            except Exception:
                continue

            if raw is None or raw.empty:
                continue

            for sym in batch:
                try:
                    if len(batch) == 1:
                        sym_df = raw
                    else:
                        sym_df = raw.get(sym)
                    if sym_df is None or sym_df.empty:
                        continue
                    sym_df = sym_df.sort_index()
                    latest = sym_df.iloc[-1]
                    prev = sym_df.iloc[-2] if len(sym_df) > 1 else latest
                    prev_close = float(prev["Close"])
                    cur_price = float(latest["Close"])
                    change_pct = ((cur_price - prev_close) / prev_close * 100) if prev_close else 0
                    volume = int(latest.get("Volume", 0) or 0)

                    frames.append({
                        "symbol": f"{sym}.US",
                        "name": "",
                        "price": cur_price,
                        "change_pct": round(change_pct, 2),
                        "change_amount": round(cur_price - prev_close, 2),
                        "volume": volume,
                        "amount": round(cur_price * volume, 2),
                        "high": float(latest.get("High", cur_price)),
                        "low": float(latest.get("Low", cur_price)),
                        "open": float(latest.get("Open", cur_price)),
                        "prev_close": prev_close,
                    })
                except Exception:
                    continue

        if not frames:
            return pd.DataFrame()
        return pd.DataFrame(frames)

    # ── daily_bars ────────────────────────────────────────────────────────

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        tickers = [s.split(".")[0] for s in symbols]
        ticker_to_sym = dict(zip(tickers, symbols))

        import yfinance as yf

        async with self._sem:
            df = await asyncio.to_thread(
                self._download_bars, yf, tickers, start, end,
            )

        if df is None or df.empty:
            return pd.DataFrame()

        df["symbol"] = df["symbol"].map(lambda t: ticker_to_sym.get(t, f"{t}.US"))
        return df

    @staticmethod
    def _download_bars(yf, tickers: list[str], start: str, end: str) -> pd.DataFrame:
        try:
            raw = yf.download(
                tickers, start=start, end=end,
                group_by="ticker", auto_adjust=False, threads=8,
            )
        except Exception:
            return pd.DataFrame()

        if raw is None or raw.empty:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        for sym in tickers:
            try:
                if len(tickers) == 1:
                    sym_df = raw
                else:
                    sym_df = raw.get(sym)
                if sym_df is None or sym_df.empty:
                    continue
                sym_df = sym_df.reset_index()
                sym_df["symbol"] = sym
                sym_df = sym_df.rename(columns={
                    "Date": "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                })
                frames.append(sym_df)
            except Exception:
                continue

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if "date" in result.columns:
            result["date"] = pd.to_datetime(result["date"]).dt.strftime("%Y-%m-%d")
        return result

    # ── universe ──────────────────────────────────────────────────────────

    @staticmethod
    def _get_universe_tickers() -> list[str]:
        """获取 S&P 500 成分股作为美股 universe。"""
        try:
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            )
            tickers = tables[0]["Symbol"].tolist()
            return [str(t).replace(".", "-") for t in tickers if str(t).strip()]
        except Exception:
            pass

        try:
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/Nasdaq-100"
            )
            tickers = tables[4]["Ticker"].tolist()
            return [str(t).replace(".", "-") for t in tickers if str(t).strip()]
        except Exception:
            pass

        return [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
            "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD", "BAC", "DIS",
            "ADBE", "CRM", "NFLX", "INTC", "CSCO", "PEP", "KO", "MRK", "ABBV",
            "ORCL", "AMD", "QCOM", "TMO", "COST", "ABT", "DHR", "NKE", "TXN",
            "PM", "BMY", "RTX", "LOW", "UPS", "MS", "SCHW", "SPGI", "BLK",
        ]
