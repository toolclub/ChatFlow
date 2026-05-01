"""Tushare Pro provider — 需要 token + 积分

积分门槛参考（来自实测）：
  -    0 分：仅注册成功
  -  100 分：stock_basic
  -  120 分：daily（A 股日线）
  - 2000 分：daily_basic（PE/PB/换手率/市值快照）
  - 5000 分：index_weight

100 积分用户只能调 stock_basic，其他方法运行时会被服务端拒（含 "没有接口" / "权限"
关键字），由 ProviderRegistry 自动回退到下一 provider。代码里不预判积分，只透传错误。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pandas as pd

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
    Stock,
)

logger = logging.getLogger("quant.tushare")


def _is_permission_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "没有接口" in msg or "权限" in msg or "积分" in msg


class TushareProvider:
    name = "tushare"
    # 类默认全集；__init__ 会拷贝到实例 capabilities，health_check 时按权限裁剪
    _DEFAULT_CAPABILITIES = {
        ProviderCapability.STOCK_LIST,
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
        ProviderCapability.INDEX_WEIGHT,
        ProviderCapability.TRADING_CALENDAR,
    }

    def __init__(
        self,
        token: str,
        priority: int = 50,
        max_concurrency: int = 4,
    ) -> None:
        if not token:
            raise ValueError("Tushare token 不能为空")
        self._token = token
        self._pro = None
        self.priority = priority
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        # 实例级 capabilities：health_check 探活后会按权限自动裁剪
        self.capabilities: set[ProviderCapability] = set(self._DEFAULT_CAPABILITIES)

    def _get_pro(self):
        if self._pro is None:
            import tushare as ts
            ts.set_token(self._token)
            self._pro = ts.pro_api()
        return self._pro

    # ── 健康检查 ───────────────────────────────────────────────────────────

    async def health_check(self) -> ProviderHealth:
        """探活 + 按权限裁剪 capabilities。

        100 积分用户只能调 stock_basic。探活会逐个测试 daily_basic / pro_bar /
        index_weight，遇权限错误就把对应 capability 从 self.capabilities 移除，
        ProviderRegistry 后续不会再把这类请求路由到 tushare。
        """
        try:
            import tushare  # noqa: F401
        except ImportError:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message="tushare 未安装",
            )

        try:
            await asyncio.to_thread(self._ping)
        except Exception as exc:
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message=f"探活失败: {type(exc).__name__}: {exc}"[:200],
            )

        # 探各能力（同步、串行；只在启动期跑一次）
        revoked: list[str] = []

        def _try(name: str, fn) -> None:
            try:
                fn()
            except Exception as exc:
                if _is_permission_error(exc):
                    revoked.append(name)
                else:
                    # 非权限错误（网络抖动等）
                    pass

        # 并行探活提升速度，但内部 _try 捕获异常
        await asyncio.to_thread(_try, "daily_basic", self._probe_daily_basic)
        await asyncio.to_thread(_try, "pro_bar", self._probe_pro_bar)
        await asyncio.to_thread(_try, "index_weight", self._probe_index_weight)

        if "daily_basic" in revoked:
            self.capabilities.discard(ProviderCapability.REALTIME_SNAPSHOT)
        if "pro_bar" in revoked:
            self.capabilities.discard(ProviderCapability.DAILY_BARS)
        if "index_weight" in revoked:
            self.capabilities.discard(ProviderCapability.INDEX_WEIGHT)

        if revoked:
            msg = f"积分不足，已移除能力：{','.join(revoked)}"
            logger.warning("tushare 自动降级 → %s", msg)
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message=msg,
            )
        return ProviderHealth(status=ProviderHealthStatus.OK)

    # ── 能力探活（探活时调用一次，确定有没有权限） ──────────────────────────

    def _probe_daily_basic(self) -> None:
        pro = self._get_pro()
        for delta in range(7):
            d = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
            df = pro.daily_basic(trade_date=d, fields="ts_code", limit=1)
            if df is not None and not df.empty:
                return
        # 7 天都没数据但也没报错（远古日期 / 网络） — 不视为权限问题
        return

    def _probe_pro_bar(self) -> None:
        import tushare as ts
        df = ts.pro_bar(
            ts_code="000001.SZ", adj="qfq",
            start_date=(datetime.now() - timedelta(days=10)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )
        if df is None:
            raise RuntimeError("pro_bar 返回 None")

    def _probe_index_weight(self) -> None:
        pro = self._get_pro()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
        df = pro.index_weight(
            index_code="000300.SH", start_date=start, end_date=end,
        )
        if df is None:
            raise RuntimeError("index_weight 返回 None")

    def _ping(self) -> None:
        pro = self._get_pro()
        df = pro.stock_basic(
            exchange="", list_status="L", fields="ts_code", limit=1,
        )
        if df is None or df.empty:
            raise RuntimeError("stock_basic 探活返回空")

    # ── 公开 API ───────────────────────────────────────────────────────────

    async def list_stocks(self, market: str = "cn_a") -> list[Stock]:
        df = await asyncio.to_thread(self._fetch_stock_basic)
        if df.empty:
            return []
        return [
            Stock(
                symbol=str(row.get("ts_code", "")),
                name=str(row.get("name", "")),
                market="cn_a",
                industry=str(row.get("industry", "")),
                list_date=str(row.get("list_date", "")),
            )
            for _, row in df.iterrows()
            if row.get("ts_code")
        ]

    def _fetch_stock_basic(self) -> pd.DataFrame:
        pro = self._get_pro()
        df = pro.stock_basic(
            exchange="", list_status="L",
            fields="ts_code,symbol,name,area,industry,list_date",
        )
        return df if df is not None else pd.DataFrame()

    async def realtime_snapshot(self, market: str = "cn_a") -> pd.DataFrame:
        if market != "cn_a":
            raise ValueError(f"Tushare 仅支持 cn_a，收到：{market}")
        return await asyncio.to_thread(self._fetch_daily_basic_latest)

    def _fetch_daily_basic_latest(self) -> pd.DataFrame:
        pro = self._get_pro()
        # 近 7 天向回探，找有 daily_basic 数据的交易日
        latest_df: pd.DataFrame | None = None
        for delta in range(7):
            d = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
            try:
                df = pro.daily_basic(
                    trade_date=d,
                    fields=(
                        "ts_code,trade_date,close,turnover_rate,volume_ratio,"
                        "pe,pb,total_mv,circ_mv"
                    ),
                )
                if df is not None and not df.empty:
                    latest_df = df
                    break
            except Exception as exc:
                if _is_permission_error(exc):
                    raise
                continue
        if latest_df is None or latest_df.empty:
            raise RuntimeError("近 7 日 daily_basic 全空")

        df = latest_df.rename(columns={
            "ts_code": "symbol",
            "close": "price",
            "total_mv": "market_cap",
            "circ_mv": "circ_market_cap",
        })

        # 单位归一：daily_basic 总市值/流通市值是「万元」→ 亿元
        for col in ("market_cap", "circ_market_cap"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce") / 1e4

        # amount/volume/涨跌幅 来自 daily（同日）
        trade_date = str(df["trade_date"].iloc[0]) if "trade_date" in df.columns else ""
        try:
            daily = pro.daily(
                trade_date=trade_date,
                fields="ts_code,vol,amount,pct_chg",
            )
            if daily is not None and not daily.empty:
                daily = daily.rename(columns={
                    "ts_code": "symbol",
                    "vol": "volume",
                    "pct_chg": "change_pct",
                })
                # daily.amount 单位是「千元」→ 亿元
                daily["amount"] = pd.to_numeric(daily["amount"], errors="coerce") / 1e5
                df = df.merge(
                    daily[["symbol", "volume", "amount", "change_pct"]],
                    on="symbol", how="left",
                )
        except Exception as exc:
            logger.debug("Tushare daily 拉取失败（不阻断）: %s", exc)

        # name + industry 来自 stock_basic
        try:
            sb = pro.stock_basic(
                exchange="", list_status="L",
                fields="ts_code,name,industry",
            )
            if sb is not None and not sb.empty:
                sb = sb.rename(columns={"ts_code": "symbol"})
                df = df.merge(sb, on="symbol", how="left")
        except Exception as exc:
            logger.debug("Tushare stock_basic 补 name 失败（不阻断）: %s", exc)

        numeric_cols = (
            "price", "turnover_rate", "volume_ratio", "pe", "pb",
            "market_cap", "circ_market_cap", "volume", "amount", "change_pct",
        )
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if trade_date and len(trade_date) == 8:
            df["as_of_date"] = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        if "trade_date" in df.columns:
            df = df.drop(columns=["trade_date"])
        return df.reset_index(drop=True)

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        errors: dict[str, int] = {}

        async def _one(sym: str) -> pd.DataFrame:
            async with self._sem:
                try:
                    df = await asyncio.to_thread(
                        self._fetch_daily, sym, start, end, adjust,
                    )
                    if df.empty:
                        return df
                    df = df.copy()
                    df["symbol"] = sym
                    return df
                except Exception as exc:
                    if _is_permission_error(exc):
                        raise
                    msg = f"{type(exc).__name__}: {exc}"[:120]
                    errors[msg] = errors.get(msg, 0) + 1
                    return pd.DataFrame()

        results = await asyncio.gather(*[_one(s) for s in symbols])
        if errors:
            for msg, count in sorted(errors.items(), key=lambda x: -x[1]):
                logger.warning("Tushare 拉取 %d 只股票日线失败: %s", count, msg)
        non_empty = [r for r in results if not r.empty]
        if not non_empty:
            return pd.DataFrame()
        return pd.concat(non_empty, ignore_index=True)

    def _fetch_daily(
        self, symbol: str, start: str, end: str, adjust: str,
    ) -> pd.DataFrame:
        import tushare as ts

        start_d = start.replace("-", "")
        end_d = end.replace("-", "")
        if adjust in ("qfq", "hfq"):
            df = ts.pro_bar(
                ts_code=symbol, adj=adjust,
                start_date=start_d, end_date=end_d,
            )
        else:
            pro = self._get_pro()
            df = pro.daily(
                ts_code=symbol, start_date=start_d, end_date=end_d,
            )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "trade_date": "date", "vol": "volume", "pct_chg": "change_pct",
        })
        if "date" in df.columns:
            df["date"] = pd.to_datetime(
                df["date"], format="%Y%m%d", errors="coerce",
            ).dt.strftime("%Y-%m-%d")
        for col in ("open", "high", "low", "close", "volume", "amount", "change_pct"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "amount" in df.columns:
            df["amount"] = df["amount"] / 1e5  # 千元 → 亿元
        return df.sort_values("date").reset_index(drop=True)

    async def index_constituents(self, index_code: str) -> list[str]:
        code_map = {
            "hs300": "000300.SH",
            "zz500": "000905.SH",
            "zz1000": "000852.SH",
        }
        code = code_map.get(index_code, index_code)
        if "." not in code:
            code = f"{code}.SH"
        return await asyncio.to_thread(self._fetch_index_weight, code)

    def _fetch_index_weight(self, index_code: str) -> list[str]:
        pro = self._get_pro()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
        df = pro.index_weight(
            index_code=index_code, start_date=start, end_date=end,
        )
        if df is None or df.empty:
            return []
        latest = df["trade_date"].max()
        df = df[df["trade_date"] == latest]
        return df["con_code"].dropna().astype(str).tolist()
