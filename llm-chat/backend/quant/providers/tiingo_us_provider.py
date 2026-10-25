"""Tiingo US Provider — 美股行情（key 制 REST，30+ 年干净 EOD）。

与 twelvedata_us 同定位：雅虎按 IP 限流，Tiingo 按 key 限流、不受源 IP 影响，作为
provider 链里雅虎之后的兜底。Tiingo 免费档提供 30+ 年美股日线 EOD，数据口碑好。

只用官方 REST /tiingo/daily/{ticker}/prices（httpx，零新依赖）。
需要 .env 配 TIINGO_API_KEY（免费注册 https://www.tiingo.com）。
未配置 → daily_bars 返回空、health_check DOWN，provider 链自动跳过。
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
import pandas as pd

from quant.execution import run_io

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
)

logger = logging.getLogger("quant.tiingo_us")

_BASE = "https://api.tiingo.com/tiingo/daily"
_TIMEOUT = 12.0


class TiingoUSProvider:
    """Tiingo 美股数据源（key 制，作为雅虎之后的兜底）。"""

    name = "tiingo_us"
    capabilities = {ProviderCapability.DAILY_BARS}
    supported_markets = {"us_stock"}

    def __init__(self, priority: int = 31, max_concurrency: int = 8) -> None:
        self.priority = priority
        self._sem = asyncio.Semaphore(max_concurrency)

    @property
    def _api_key(self) -> str:
        return os.environ.get("TIINGO_API_KEY", "").strip()

    async def health_check(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message="未配置 TIINGO_API_KEY",
            )
        return ProviderHealth(status=ProviderHealthStatus.OK, message="key 已配置")

    async def realtime_snapshot(self, market: str = "us_stock") -> pd.DataFrame:
        return pd.DataFrame()

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        key = self._api_key
        if not symbols or not key:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        for sym in symbols:
            ticker = sym.split(".")[0]
            async with self._sem:
                df = await run_io(
                    self._fetch_one, ticker, start, end, key,
                )
            if df is not None and not df.empty:
                df["symbol"] = sym
                frames.append(df)

        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @staticmethod
    def _fetch_one(ticker: str, start: str, end: str, key: str) -> pd.DataFrame:
        """GET /{ticker}/prices → 归一化列 date/open/high/low/close/volume。

        Tiingo 返回 [{date, open, high, low, close, volume, adjClose, ...}, ...]（时间升序）。
        adjust="qfq" 时优先用 adjClose/adj* 前复权列（与雅虎 auto_adjust=False 一致口径按需）。
        错误时返回 {"detail": "..."} 而非数组。
        """
        params = {
            "startDate": start,
            "endDate": end,
            "format": "json",
            "token": key,
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = httpx.get(
                f"{_BASE}/{ticker}/prices", params=params, headers=headers, timeout=_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("tiingo_us %s 请求异常: %s", ticker, exc)
            return pd.DataFrame()

        if resp.status_code != 200:
            logger.warning("tiingo_us %s → HTTP %s", ticker, resp.status_code)
            return pd.DataFrame()

        try:
            body = resp.json()
        except Exception as exc:
            logger.warning("tiingo_us %s JSON 解析失败: %s", ticker, exc)
            return pd.DataFrame()

        if not isinstance(body, list) or not body:
            if isinstance(body, dict):
                logger.warning("tiingo_us %s 接口报错: %s", ticker, body.get("detail", body))
            return pd.DataFrame()

        df = pd.DataFrame(body)
        # 优先用未复权 OHLC（与其它 provider 口径对齐；下游 forecast 只要相对走势）
        col_map = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
        for src, dst in col_map.items():
            if src in df.columns:
                df[dst] = pd.to_numeric(df[src], errors="coerce")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].dropna(subset=["close"]).reset_index(drop=True)
        logger.info("tiingo_us %s → %d 根", ticker, len(df))
        return df
