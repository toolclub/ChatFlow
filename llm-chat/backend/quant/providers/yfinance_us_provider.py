"""YFinance US Provider — 美股数据源（海外直连，稳定）

通过 Yahoo Finance API 获取美股行情和 K 线。需要 VPN。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

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
                        "name": _TICKER_TO_CN_NAME.get(sym, ""),
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
        # yfinance.download 的 `end` 参数是 exclusive — 不包含 end 当日。
        # 调用方传 end=今天，期望得到含今天的数据；这里 +1 天才能正确包含。
        end_inclusive = end
        try:
            end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            end_inclusive = end_dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass

        try:
            raw = yf.download(
                tickers, start=start, end=end_inclusive,
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
                if isinstance(raw.columns, pd.MultiIndex):
                    # 多 ticker（或新版 yfinance 单 ticker）→ MultiIndex；按 ticker 切片
                    if sym in raw.columns.get_level_values(1):
                        sym_df = raw.xs(sym, axis=1, level=1)
                    elif len(tickers) == 1:
                        # 单 ticker 但 ticker 不在 level=1：可能 level=0；展平取该 ticker 列
                        sym_df = raw.copy()
                        sym_df.columns = sym_df.columns.get_level_values(0)
                    else:
                        sym_df = None
                elif len(tickers) == 1:
                    # 单层 columns（旧版 yfinance 单 ticker 行为）
                    sym_df = raw
                else:
                    sym_df = raw.get(sym)

                if sym_df is None or sym_df.empty:
                    continue
                sym_df = sym_df.reset_index()
                # 防御：reset_index 后若 columns 仍是 MultiIndex，硬性展平到第 0 级
                if isinstance(sym_df.columns, pd.MultiIndex):
                    sym_df.columns = [c[0] if isinstance(c, tuple) else c for c in sym_df.columns]
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

    # ── universe（多源 fallback，全免费） ─────────────────────────────────────

    @staticmethod
    def _get_universe_tickers() -> list[str]:
        """免费多源获取美股 universe（按优先级 fallback）：

          1. akshare `stock_us_spot_em` —— 东方财富全美股快照（5000+ 只），按成交额取 top N
          2. stockanalysis.com —— S&P 500 + NASDAQ-100（~600 只去重）
          3. Wikipedia + User-Agent —— 绕反爬抓 S&P 500 / NASDAQ-100
          4. 内置 45 只兜底

        返回 Yahoo 格式 ticker（`.` 替换为 `-`，例如 BRK.B → BRK-B）。
        """
        from quant.config import QUANT_YFINANCE_US_UNIVERSE_SIZE
        size = max(45, int(QUANT_YFINANCE_US_UNIVERSE_SIZE))

        # 预先注册 45 只大盘股内置中文名（保底）；akshare 源后续会补充更多
        _register_cn_names(_BUILTIN_FALLBACK_NAMES)

        for src_name, src_fn in (
            ("akshare",      _universe_from_akshare),
            ("stockanalysis", _universe_from_stockanalysis),
            ("wikipedia",    _universe_from_wikipedia),
        ):
            try:
                tickers = src_fn(size)
            except Exception as exc:
                logger.warning("yfinance_us: universe 来源 %s 异常: %s", src_name, exc)
                continue
            if tickers:
                logger.info("yfinance_us: universe 来源=%s，得到 %d 只", src_name, len(tickers))
                return tickers[:size]

        fallback = _BUILTIN_FALLBACK_TICKERS
        # 同时注册内置中文名，让前端展示中文
        _register_cn_names(_BUILTIN_FALLBACK_NAMES)
        logger.warning("yfinance_us: 全部源失败，使用内置 fallback 列表，%d 只", len(fallback))
        return fallback


# ── 内置 45 只兜底（带中文名，只在所有免费源都失败时用）─────────────────────

_BUILTIN_FALLBACK_NAMES: dict[str, str] = {
    "AAPL": "苹果", "MSFT": "微软", "GOOGL": "谷歌", "AMZN": "亚马逊",
    "NVDA": "英伟达", "META": "Meta", "TSLA": "特斯拉", "BRK-B": "伯克希尔哈撒韦",
    "JPM": "摩根大通", "V": "Visa", "JNJ": "强生", "WMT": "沃尔玛",
    "PG": "宝洁", "MA": "万事达", "UNH": "联合健康", "HD": "家得宝",
    "BAC": "美国银行", "DIS": "迪士尼", "ADBE": "Adobe", "CRM": "Salesforce",
    "NFLX": "奈飞", "INTC": "英特尔", "CSCO": "思科", "PEP": "百事",
    "KO": "可口可乐", "MRK": "默克", "ABBV": "艾伯维", "ORCL": "甲骨文",
    "AMD": "AMD", "QCOM": "高通", "TMO": "赛默飞", "COST": "好市多",
    "ABT": "雅培", "DHR": "丹纳赫", "NKE": "耐克", "TXN": "德州仪器",
    "PM": "菲利普莫里斯", "BMY": "百时美施贵宝", "RTX": "雷神技术",
    "LOW": "劳氏", "UPS": "联合包裹", "MS": "摩根士丹利", "SCHW": "嘉信理财",
    "SPGI": "标普全球", "BLK": "贝莱德",
}

_BUILTIN_FALLBACK_TICKERS = list(_BUILTIN_FALLBACK_NAMES.keys())


# 模块级 ticker → 中文公司名映射缓存。
# 由 universe 抓取阶段填充（akshare 源天然有中文名；fallback 用内置映射），
# spot 拉取后用此 dict 覆盖 name 字段，让前端展示中文而非代号。
_TICKER_TO_CN_NAME: dict[str, str] = {}


def _register_cn_names(mapping: dict[str, str]) -> None:
    """把 ticker → 中文名写入模块级缓存（已有的不覆盖，保留先来源数据）。"""
    for k, v in mapping.items():
        if k and v and k not in _TICKER_TO_CN_NAME:
            _TICKER_TO_CN_NAME[k] = v


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ── universe 来源 1：akshare 全美股快照 ──────────────────────────────────────

def _universe_from_akshare(size: int) -> list[str]:
    import akshare as ak

    raw = ak.stock_us_spot_em()
    if raw is None or raw.empty:
        return []

    df = raw.copy()
    # 按成交额降序（拉前 size 只热门股，K 线下载更有价值）
    if "成交额" in df.columns:
        df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
        df = df.sort_values("成交额", ascending=False, na_position="last")

    code_col = "代码" if "代码" in df.columns else "em_code"
    name_col = "名称" if "名称" in df.columns else None  # akshare 返回的中文公司名

    tickers: list[str] = []
    cn_names: dict[str, str] = {}
    seen: set[str] = set()
    for _, row in df.iterrows():
        em_code = str(row[code_col])
        # em_code 形如 "105.AAPL" 或 "105.BRK.B"；split 第 1 个 . 取后半
        if "." not in em_code:
            continue
        ticker_raw = em_code.split(".", 1)[1].strip()
        # Yahoo 用 "-" 代替 "."（BRK.B → BRK-B）
        ticker_yahoo = ticker_raw.replace(".", "-")
        if not ticker_yahoo or ticker_yahoo in seen:
            continue
        seen.add(ticker_yahoo)
        tickers.append(ticker_yahoo)
        if name_col is not None:
            name = str(row[name_col]).strip()
            if name and name != "nan":
                cn_names[ticker_yahoo] = name
        if len(tickers) >= size:
            break
    # 把中文名注入模块级缓存，供 spot 阶段填充 name 字段
    if cn_names:
        _register_cn_names(cn_names)
    return tickers


# ── universe 来源 2：stockanalysis.com（免费，无反爬）────────────────────────

def _universe_from_stockanalysis(size: int) -> list[str]:
    import httpx

    urls = (
        "https://stockanalysis.com/list/sp-500-stocks/",
        "https://stockanalysis.com/list/nasdaq-100-stocks/",
    )
    tickers: list[str] = []
    seen: set[str] = set()
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=10.0, headers=headers, follow_redirects=True) as client:
        for url in urls:
            try:
                r = client.get(url)
                r.raise_for_status()
                from io import StringIO
                tables = pd.read_html(StringIO(r.text))
            except Exception as exc:
                logger.warning("yfinance_us: stockanalysis 抓 %s 失败: %s", url, exc)
                continue
            for tbl in tables:
                col = next((c for c in tbl.columns if str(c).strip().lower() == "symbol"), None)
                if col is None:
                    continue
                for sym in tbl[col].astype(str):
                    sym = sym.strip().replace(".", "-")
                    if sym and sym not in seen and not sym.startswith("Unnamed"):
                        seen.add(sym)
                        tickers.append(sym)
                break
            if len(tickers) >= size:
                break
    return tickers


# ── universe 来源 3：Wikipedia + User-Agent（绕反爬） ────────────────────────

def _universe_from_wikipedia(size: int) -> list[str]:
    import httpx

    sources = (
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", 0),
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", 4),
    )
    tickers: list[str] = []
    seen: set[str] = set()
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=10.0, headers=headers, follow_redirects=True) as client:
        for url, col_name, table_idx in sources:
            try:
                r = client.get(url)
                r.raise_for_status()
                from io import StringIO
                tables = pd.read_html(StringIO(r.text))
            except Exception as exc:
                logger.warning("yfinance_us: Wikipedia 抓 %s 失败: %s", url, exc)
                continue
            if table_idx >= len(tables):
                continue
            tbl = tables[table_idx]
            col = next((c for c in tbl.columns if str(c).strip() == col_name), None)
            if col is None:
                continue
            for sym in tbl[col].astype(str):
                sym = sym.strip().replace(".", "-")
                if sym and sym not in seen:
                    seen.add(sym)
                    tickers.append(sym)
            if len(tickers) >= size:
                break
    return tickers
