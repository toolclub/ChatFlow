"""YFinance US Provider — 美股数据源（海外直连，稳定）

通过 Yahoo Finance API 获取美股行情和 K 线。需要 VPN。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import pandas as pd

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
)

logger = logging.getLogger("quant.yfinance_us")


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
            logger.warning("yfinance_us: universe 为空，无法拉取 spot")
            return pd.DataFrame()

        logger.info("yfinance_us: 开始拉取美股 spot | ticker 总数 %d", len(tickers))
        t0 = time.perf_counter()
        df = await asyncio.to_thread(self._download_spot, tickers, yf)
        elapsed = time.perf_counter() - t0
        logger.info(
            "yfinance_us: spot 拉取完成 | 得到 %d 只 | 耗时 %.1fs",
            len(df), elapsed,
        )
        if df is None or df.empty:
            return pd.DataFrame()

        df["market"] = "us_stock"
        df["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
        return df

    @staticmethod
    def _download_spot(tickers: list[str], yf) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        batch_size = 100

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            batch_idx = i // batch_size + 1
            total_batches = (len(tickers) + batch_size - 1) // batch_size
            try:
                raw = yf.download(
                    batch, period="5d",
                    auto_adjust=False, threads=4,
                )
            except Exception as exc:
                logger.warning(
                    "yfinance_us spot 批次 %d/%d download 异常: %s",
                    batch_idx, total_batches, exc,
                )
                continue

            if raw is None or raw.empty:
                logger.warning(
                    "yfinance_us spot 批次 %d/%d 返回空",
                    batch_idx, total_batches,
                )
                continue

            # yf.download 多 ticker 返回 MultiIndex columns: (price_type, ticker)
            # 或者 Index columns (单 ticker)
            for sym in batch:
                try:
                    if len(batch) == 1:
                        sym_df = raw
                    elif isinstance(raw.columns, pd.MultiIndex):
                        # columns: (Open, AAPL), (High, AAPL), ... → 取 level=1
                        sym_df = raw.xs(sym, axis=1, level=1)
                    else:
                        sym_df = raw.get(sym)
                        if sym_df is None:
                            sym_df = raw.xs(sym, axis=1, level=1) if sym in raw.columns.get_level_values(1) else None

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

            # 批次间小睡，避免触发 rate limit
            if i + batch_size < len(tickers):
                time.sleep(1.5)

        if not frames:
            logger.warning("yfinance_us spot: 所有批次均无数据")
            return pd.DataFrame()

        df = pd.DataFrame(frames)
        logger.info(
            "yfinance_us spot: 成功 %d/%d 只 (%.0f%%)",
            len(df), len(tickers), len(df) / max(len(tickers), 1) * 100,
        )
        return df

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
                auto_adjust=False, threads=8,
            )
        except Exception as exc:
            logger.warning("yfinance_us bars download 异常: %s", exc)
            return pd.DataFrame()

        if raw is None or raw.empty:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        for sym in tickers:
            try:
                if len(tickers) == 1:
                    sym_df = raw
                elif isinstance(raw.columns, pd.MultiIndex):
                    sym_df = raw.xs(sym, axis=1, level=1)
                else:
                    sym_df = raw.get(sym)
                    if sym_df is None:
                        sym_df = raw.xs(sym, axis=1, level=1) if sym in raw.columns.get_level_values(1) else None

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
            result = [str(t).replace(".", "-") for t in tickers if str(t).strip()]
            logger.info("yfinance_us: Wikipedia S&P 500 获取成功，%d 只", len(result))
            return result
        except Exception as exc:
            logger.warning("yfinance_us: Wikipedia S&P 500 获取失败: %s", exc)

        try:
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/Nasdaq-100"
            )
            tickers = tables[4]["Ticker"].tolist()
            result = [str(t).replace(".", "-") for t in tickers if str(t).strip()]
            logger.info("yfinance_us: Wikipedia NASDAQ-100 获取成功，%d 只", len(result))
            return result
        except Exception as exc:
            logger.warning("yfinance_us: Wikipedia NASDAQ-100 获取失败: %s", exc)

        fallback = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
            "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD", "BAC", "DIS",
            "ADBE", "CRM", "NFLX", "INTC", "CSCO", "PEP", "KO", "MRK", "ABBV",
            "ORCL", "AMD", "QCOM", "TMO", "COST", "ABT", "DHR", "NKE", "TXN",
            "PM", "BMY", "RTX", "LOW", "UPS", "MS", "SCHW", "SPGI", "BLK",
        ]
        logger.warning("yfinance_us: 使用内置 fallback 列表，%d 只", len(fallback))
        return fallback
