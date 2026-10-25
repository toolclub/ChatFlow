"""Twelve Data US Provider — 美股行情（key 制 REST，不吃雅虎那种 IP 限流）。

为什么加它：yfinance/雅虎按 IP 限流，被重度标记的生产 IP 会稳定 429。Twelve Data 是
**带 API key** 的服务，限流按 key 算（免费 800 次/天、8 次/分），对"偶尔预测一支股"
远远够用，且不受源 IP 影响。作为雅虎 chart 之后的兜底 provider 接入 provider 链。

只用官方 REST 的 /time_series 接口（httpx，零新依赖，与项目已有的 Tavily/雅虎 chart 同款做法），
不引 twelvedata SDK（那只是薄封装）。

需要 .env 配 TWELVEDATA_API_KEY（免费注册 https://twelvedata.com，10 秒）。
未配置时 daily_bars 返回空、health_check 报 DOWN —— provider 链自动跳过它，不报错。
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

logger = logging.getLogger("quant.twelvedata_us")

_BASE = "https://api.twelvedata.com"
_TIMEOUT = 12.0


class TwelveDataUSProvider:
    """Twelve Data 美股数据源（key 制，作为雅虎之后的兜底）。"""

    name = "twelvedata_us"
    capabilities = {ProviderCapability.DAILY_BARS}
    supported_markets = {"us_stock"}

    def __init__(self, priority: int = 30, max_concurrency: int = 8) -> None:
        self.priority = priority
        self._sem = asyncio.Semaphore(max_concurrency)

    @property
    def _api_key(self) -> str:
        # 每次读 os.environ：key 由 decrypt_env 在 config import 时解密写回，
        # 构造 provider 时可能还没解密完，所以延迟到调用时取。
        return os.environ.get("TWELVEDATA_API_KEY", "").strip()

    async def health_check(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message="未配置 TWELVEDATA_API_KEY",
            )
        # 不真打接口（省额度）；有 key 即视为可用，真失败由 daily_bars 兜底链处理。
        return ProviderHealth(status=ProviderHealthStatus.OK, message="key 已配置")

    async def realtime_snapshot(self, market: str = "us_stock") -> pd.DataFrame:
        # 预测只用日线；实时快照留空（provider 链只按 DAILY_BARS 能力挑它）。
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
        """GET /time_series → 归一化列 date/open/high/low/close/volume。

        Twelve Data 返回 {"values": [{datetime, open, high, low, close, volume}, ...],
        "status": "ok"}，values 按时间**倒序**。错误时 {"status":"error","message":...}。
        """
        params = {
            "symbol": ticker,
            "interval": "1day",
            "start_date": start,
            "end_date": end,
            "outputsize": "5000",        # 上限，覆盖 2 年（~500 根）绰绰有余
            "order": "ASC",
            "apikey": key,
        }
        try:
            resp = httpx.get(f"{_BASE}/time_series", params=params, timeout=_TIMEOUT)
        except Exception as exc:
            logger.warning("twelvedata_us %s 请求异常: %s", ticker, exc)
            return pd.DataFrame()

        if resp.status_code != 200:
            logger.warning("twelvedata_us %s → HTTP %s", ticker, resp.status_code)
            return pd.DataFrame()

        try:
            body = resp.json()
        except Exception as exc:
            logger.warning("twelvedata_us %s JSON 解析失败: %s", ticker, exc)
            return pd.DataFrame()

        if body.get("status") == "error":
            # 额度耗尽 / key 无效 / symbol 不存在 —— 如实记原因，返回空让链继续
            logger.warning("twelvedata_us %s 接口报错: %s", ticker, body.get("message", ""))
            return pd.DataFrame()

        values = body.get("values") or []
        if not values:
            return pd.DataFrame()

        df = pd.DataFrame(values)
        rename = {"datetime": "date"}
        df = df.rename(columns=rename)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].dropna(subset=["close"]).reset_index(drop=True)
        logger.info("twelvedata_us %s → %d 根", ticker, len(df))
        return df
