"""AKShare provider — 免 token，作为 v1 默认数据源

注意：所有 AKShare 调用都是同步的，必须通过 asyncio.to_thread 包装。

实战观察：东方财富 `stock_zh_a_spot_em` 内部用 `requests` 库，被服务端
持续 RST（`RemoteDisconnected`）。但同样的 URL 用 `httpx` / `urllib` /
`curl` 是通的。因此本 provider 的 _fetch_spot_em 不直接调 ak，而是：
  1. 先用 httpx 直连 EM clist API + 重试 → 全字段（PE/PB/市值/动量等）
  2. 失败时降级到 Sina (`ak.stock_zh_a_spot`) → 仅基础行情，PE/PB/市值缺失
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from datetime import datetime

import pandas as pd

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
    Stock,
)
from quant.providers.symbols import to_akshare_code, to_internal

logger = logging.getLogger("quant.akshare")


_SPOT_RENAME = {
    "代码": "_code6",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amount",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "prev_close",
    "量比": "volume_ratio",
    "换手率": "turnover_rate",
    "市盈率-动态": "pe",
    "市净率": "pb",
    "总市值": "market_cap",
    "流通市值": "circ_market_cap",
    "60日涨跌幅": "change_pct_60d",
    "年初至今涨跌幅": "change_pct_ytd",
}

_HIST_RENAME = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amount",
    "换手率": "turnover_rate",
}


class AKShareProvider:
    """AKShare 数据源适配器。"""

    name = "akshare"
    capabilities = {
        ProviderCapability.STOCK_LIST,
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
        ProviderCapability.INDEX_WEIGHT,
        ProviderCapability.TRADING_CALENDAR,
    }

    def __init__(self, priority: int = 100, max_concurrency: int = 8) -> None:
        self.priority = priority
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    # ── 健康检查 ───────────────────────────────────────────────────────────

    async def health_check(self) -> ProviderHealth:
        try:
            import akshare  # noqa: F401
        except ImportError:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message="akshare 未安装（pip install akshare）",
            )
        try:
            await asyncio.to_thread(self._ping)
            return ProviderHealth(status=ProviderHealthStatus.OK)
        except Exception as exc:
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message=f"探活失败：{type(exc).__name__}: {exc}"[:200],
            )

    @staticmethod
    def _ping() -> None:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            raise RuntimeError("交易日历为空")

    # ── 列表 / 快照 ────────────────────────────────────────────────────────

    async def list_stocks(self, market: str = "cn_a") -> list[Stock]:
        df = await self.realtime_snapshot(market=market)
        if df.empty:
            return []
        return [
            Stock(symbol=row["symbol"], name=str(row.get("name", "")), market="cn_a")
            for _, row in df.iterrows()
        ]

    async def realtime_snapshot(self, market: str = "cn_a") -> pd.DataFrame:
        if market != "cn_a":
            raise ValueError(f"AKShare provider 暂只支持 cn_a，收到：{market}")
        return await asyncio.to_thread(self._fetch_spot_em)

    @staticmethod
    def _fetch_spot_em() -> pd.DataFrame:
        """优先 httpx 直连东方财富 clist API；连续失败时退到 Sina spot。"""
        df = AKShareProvider._try_em_via_httpx()
        if df is not None and not df.empty:
            return df
        logger.warning("EM clist 不可用，降级 Sina spot（PE/PB/市值/换手率不可用）")
        return AKShareProvider._fetch_spot_sina()

    # ── EM clist via httpx（直连，绕过 AKShare 内部 requests） ────────────

    _EM_FIELD_MAP = {
        "f12": "_code6", "f14": "name", "f2": "price",
        "f3": "change_pct", "f4": "change_amount",
        "f5": "volume", "f6": "amount", "f7": "amplitude",
        "f8": "turnover_rate", "f9": "pe", "f10": "volume_ratio",
        "f15": "high", "f16": "low", "f17": "open", "f18": "prev_close",
        "f20": "market_cap", "f21": "circ_market_cap",
        "f22": "speed", "f23": "pb",
        "f24": "change_pct_60d", "f25": "change_pct_ytd",
    }

    @staticmethod
    def _try_em_via_httpx() -> pd.DataFrame | None:
        try:
            import httpx
        except ImportError:
            logger.warning("httpx 未安装，跳过 EM 直连路径")
            return None

        url = "https://82.push2.eastmoney.com/api/qt/clist/get"
        base_params = {
            "pn": "1", "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f12",
            "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
            "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
                      "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Connection": "close",
            "Referer": "https://quote.eastmoney.com/",
        }

        def _get(params: dict, max_attempts: int = 5) -> dict | None:
            last_exc: Exception | None = None
            for i in range(max_attempts):
                try:
                    with httpx.Client(timeout=15.0, http2=False, headers=headers) as c:
                        r = c.get(url, params=params)
                        r.raise_for_status()
                        return r.json()
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.5 * (2 ** i) + random.random() * 0.5)
            logger.debug("EM clist 单页重试 %d 次仍失败: %s", max_attempts, last_exc)
            return None

        first = _get(base_params, max_attempts=6)
        if first is None:
            return None
        diff = (first.get("data") or {}).get("diff") or []
        if not diff:
            return None
        per_page = len(diff)
        total = (first.get("data") or {}).get("total") or per_page
        total_pages = math.ceil(total / per_page)

        rows = list(diff)
        failed_pages = 0
        for page in range(2, total_pages + 1):
            p = base_params.copy()
            p["pn"] = str(page)
            d = _get(p)
            if d is None:
                failed_pages += 1
                if failed_pages >= 4:
                    logger.warning(
                        "EM clist 已累计 %d 页失败，放弃直连（已拉 %d 行）",
                        failed_pages, len(rows),
                    )
                    return None
                continue
            rows.extend((d.get("data") or {}).get("diff") or [])
            time.sleep(0.4 + random.random() * 0.4)

        if not rows:
            return None
        return AKShareProvider._normalize_em_rows(rows)

    @staticmethod
    def _normalize_em_rows(rows: list[dict]) -> pd.DataFrame:
        raw = pd.DataFrame(rows)
        out = pd.DataFrame()
        for fcol, name in AKShareProvider._EM_FIELD_MAP.items():
            if fcol in raw.columns:
                out[name] = raw[fcol]

        numeric_cols = [
            "price", "change_pct", "change_amount", "volume", "amount",
            "amplitude", "high", "low", "open", "prev_close",
            "volume_ratio", "turnover_rate", "pe", "pb",
            "market_cap", "circ_market_cap", "change_pct_60d", "change_pct_ytd",
        ]
        for col in numeric_cols:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")

        for col in ("market_cap", "circ_market_cap", "amount"):
            if col in out.columns:
                out[col] = out[col] / 1e8

        if "_code6" in out.columns:
            out["symbol"] = out["_code6"].astype(str).str.zfill(6).map(to_internal)
            out = out.drop(columns=["_code6"])
        else:
            out["symbol"] = ""

        out = out[out["symbol"].astype(bool)].reset_index(drop=True)
        out["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
        return out

    # ── Sina spot 兜底（PE/PB/市值缺失） ────────────────────────────────

    @staticmethod
    def _fetch_spot_sina() -> pd.DataFrame:
        import akshare as ak

        raw = ak.stock_zh_a_spot()
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["symbol", "name"])

        rename = {
            "代码": "_sina_code", "名称": "name", "最新价": "price",
            "涨跌额": "change_amount", "涨跌幅": "change_pct",
            "成交量": "volume", "成交额": "amount",
            "最高": "high", "最低": "low", "今开": "open", "昨收": "prev_close",
        }
        df = raw.rename(columns={k: v for k, v in rename.items() if k in raw.columns})

        numeric_cols = [
            "price", "change_amount", "change_pct",
            "volume", "amount", "high", "low", "open", "prev_close",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "amount" in df.columns:
            df["amount"] = df["amount"] / 1e8

        if "_sina_code" in df.columns:
            def _conv(code: object) -> str:
                s = str(code or "").strip().lower()
                if s.startswith("sh"):
                    return f"{s[2:]}.SH"
                if s.startswith("sz"):
                    return f"{s[2:]}.SZ"
                if s.startswith("bj"):
                    return f"{s[2:]}.BJ"
                return ""
            df["symbol"] = df["_sina_code"].map(_conv)
            df = df.drop(columns=["_sina_code"])
        else:
            df["symbol"] = ""

        df = df[df["symbol"].astype(bool)].reset_index(drop=True)
        df["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
        return df

    # ── 历史 K 线 ──────────────────────────────────────────────────────────

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        async def _one(sym: str) -> pd.DataFrame:
            async with self._sem:
                try:
                    df = await asyncio.to_thread(self._fetch_hist, sym, start, end, adjust)
                    if df.empty:
                        return df
                    df = df.copy()
                    df["symbol"] = sym
                    return df
                except Exception as exc:
                    logger.warning("AKShare 拉取 %s 日线失败: %s", sym, exc)
                    return pd.DataFrame()

        results = await asyncio.gather(*[_one(s) for s in symbols])
        non_empty = [r for r in results if not r.empty]
        if not non_empty:
            return pd.DataFrame()
        return pd.concat(non_empty, ignore_index=True)

    @staticmethod
    def _fetch_hist(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
        import akshare as ak

        code6 = to_akshare_code(symbol)
        start_d = start.replace("-", "")
        end_d = end.replace("-", "")
        raw = ak.stock_zh_a_hist(
            symbol=code6, period="daily",
            start_date=start_d, end_date=end_d,
            adjust=adjust,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = raw.rename(columns={k: v for k, v in _HIST_RENAME.items() if k in raw.columns})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in (
            "open", "close", "high", "low", "volume", "amount",
            "amplitude", "change_pct", "turnover_rate",
        ):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ── 指数成分股 ─────────────────────────────────────────────────────────

    async def index_constituents(self, index_code: str) -> list[str]:
        code_map = {"hs300": "000300", "zz500": "000905", "zz1000": "000852"}
        code = code_map.get(index_code, index_code)
        return await asyncio.to_thread(self._fetch_index_cons, code)

    @staticmethod
    def _fetch_index_cons(code: str) -> list[str]:
        import akshare as ak

        df = None
        for fn_name in ("index_stock_cons_csindex", "index_stock_cons"):
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            try:
                df = fn(symbol=code)
                if df is not None and not df.empty:
                    break
            except Exception as exc:
                logger.debug("AKShare %s(%s) 失败: %s", fn_name, code, exc)
                df = None
        if df is None or df.empty:
            return []
        for col in ("成分券代码", "成分股代码", "code", "品种代码"):
            if col in df.columns:
                return [
                    s for s in (
                        to_internal(str(v).zfill(6)) for v in df[col]
                    ) if s
                ]
        return []
