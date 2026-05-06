"""BaoStock provider — 免 token A 股数据，第一阶段主源

设计要点：
  - 登录态进程级，使用 asyncio.Lock 串行化所有调用（baostock 非线程安全）
  - 不提供全市场实时快照（baostock 没有这种 API）→ 不声明 REALTIME_SNAPSHOT
  - 优势：日线带 peTTM/pbMRQ/turn 指标，service 层用 bars 兜底缺失的 PE/PB
  - 同步 SDK 全部用 asyncio.to_thread 包装
  - 关键超时设计（解决长时间 hang）：
    * 模块加载时设置 socket.setdefaulttimeout(15)，确保 bs.login() 之前生效，
      新建 TCP 连接才能继承超时——之前只在 login 后设置等同于无效。
    * 每个 batch 用 asyncio.wait_for 加硬超时，超时后强制 logout 重置连接，
      让下次调用走干净的新连接，避免一次卡死污染整个进程。
"""
from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timedelta

import pandas as pd

# 进程级网络超时：必须在任何 baostock 调用（含 bs.login）之前设置
socket.setdefaulttimeout(15)

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
    Stock,
)

logger = logging.getLogger("quant.baostock")


class BaoStockProvider:
    name = "baostock"
    capabilities = {
        ProviderCapability.STOCK_LIST,
        ProviderCapability.DAILY_BARS,
        ProviderCapability.INDEX_WEIGHT,
        ProviderCapability.TRADING_CALENDAR,
    }
    supported_markets = {"cn_a"}

    _BARS_FIELDS = (
        "date,code,open,high,low,close,preclose,volume,amount,"
        "turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST,tradestatus"
    )

    def __init__(self, priority: int = 30) -> None:
        self.priority = priority
        self._lock = asyncio.Lock()
        self._logged_in = False

    # ── 健康检查 ───────────────────────────────────────────────────────────

    async def health_check(self) -> ProviderHealth:
        try:
            import baostock  # noqa: F401
        except ImportError:
            return ProviderHealth(
                status=ProviderHealthStatus.DOWN,
                message="baostock 未安装",
            )
        try:
            ok = await self._call_sync(self._login_if_needed)
            if not ok:
                return ProviderHealth(
                    status=ProviderHealthStatus.DOWN,
                    message="baostock 登录失败",
                )
            return ProviderHealth(status=ProviderHealthStatus.OK)
        except Exception as exc:
            return ProviderHealth(
                status=ProviderHealthStatus.DEGRADED,
                message=f"探活失败: {type(exc).__name__}: {exc}"[:200],
            )

    # ── 公开 API ───────────────────────────────────────────────────────────

    async def list_stocks(self, market: str = "cn_a") -> list[Stock]:
        df = await self._call_sync(self._fetch_all_stock)
        if df.empty:
            return []
        return [
            Stock(symbol=row["symbol"], name=str(row.get("name", "")), market="cn_a")
            for _, row in df.iterrows()
        ]

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        
        # 快速过滤掉不支持的代码（如 .US），避免无效加锁
        cn_symbols = [s for s in symbols if s.endswith((".SH", ".SZ", ".BJ"))]
        if not cn_symbols:
            return pd.DataFrame()
            
        adjust_flag = {"qfq": "2", "hfq": "1"}.get(adjust, "3")

        # 核心修复：分批锁定，防止长耗时预热任务饿死其他请求
        # batch 越小，单次 RPC hang 的爆炸半径越小（最多浪费 BATCH_SIZE × 单符号超时）
        BATCH_SIZE = 10
        # 单 batch 硬超时：每符号 5s × 10 = 50s，留 10s 缓冲
        BATCH_TIMEOUT = 60.0
        all_results: list[pd.DataFrame] = []
        sym_list = list(cn_symbols)
        total_batches = (len(sym_list) + BATCH_SIZE - 1) // BATCH_SIZE
        done_count = 0
        fail_count = 0

        for i in range(0, len(sym_list), BATCH_SIZE):
            batch = sym_list[i : i + BATCH_SIZE]
            batch_no = i // BATCH_SIZE + 1
            try:
                df_batch = await asyncio.wait_for(
                    self._call_sync(self._fetch_bars_batch, batch, start, end, adjust_flag),
                    timeout=BATCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("baostock 批次 %d/%d 超时 %.0fs，重置连接跳过 %d 只",
                               batch_no, total_batches, BATCH_TIMEOUT, len(batch))
                self._force_relogin()
                fail_count += len(batch)
                df_batch = pd.DataFrame()
            except Exception as exc:
                logger.warning("baostock 批次 %d/%d 异常: %s", batch_no, total_batches, exc)
                fail_count += len(batch)
                df_batch = pd.DataFrame()

            batch_done = df_batch["symbol"].nunique() if not df_batch.empty and "symbol" in df_batch.columns else 0
            done_count += batch_done
            fail_count += max(0, len(batch) - batch_done) if not df_batch.empty else 0
            if not df_batch.empty:
                all_results.append(df_batch)
                # 增量写盘：每批完成立刻落盘，筛选请求在预热中途就能命中缓存
                try:
                    from quant import cache_disk as _cd
                    await _cd.write_bars("cn_a", df_batch)
                except Exception:
                    pass

            if batch_no % 20 == 1 or batch_no == total_batches:
                logger.info("baostock 进度 %d/%d 只 (批次 %d/%d, 失败 %d)",
                            done_count, len(sym_list), batch_no, total_batches, fail_count)

            # 在批次之间主动让出 CPU / 事件循环，给其他请求插队机会
            if i + BATCH_SIZE < len(sym_list):
                await asyncio.sleep(0.05)

        if not all_results:
            return pd.DataFrame()
        return pd.concat(all_results, ignore_index=True)

    async def index_constituents(self, index_code: str) -> list[str]:
        return await self._call_sync(self._fetch_index_cons, index_code)

    # ── 同步执行 + 串行锁 ─────────────────────────────────────────────────

    async def _call_sync(self, fn, *args, **kwargs):
        async with self._lock:
            def _run():
                if not self._login_if_needed():
                    raise RuntimeError("BaoStock 登录失败")
                return fn(*args, **kwargs)
            return await asyncio.to_thread(_run)

    def _login_if_needed(self) -> bool:
        if self._logged_in:
            return True
        import baostock as bs

        result = bs.login()
        code = getattr(result, "error_code", "")
        msg = getattr(result, "error_msg", "")
        if str(code) != "0":
            logger.warning("BaoStock 登录失败 code=%s msg=%s", code, msg)
            return False
        self._logged_in = True
        logger.info("BaoStock 登录成功")
        return True

    def _force_relogin(self) -> None:
        """超时/异常后调用：标记登录失效，下次 _login_if_needed 会重新建连。

        注意：不主动 bs.logout()——卡死的旧线程可能还在用旧连接，主动 logout 会
        触发 bs SDK 的全局状态错乱。这里只翻转标记，让下次 login 走新连接。
        """
        self._logged_in = False

    # ── baostock 同步实现 ─────────────────────────────────────────────────

    def _fetch_all_stock(self) -> pd.DataFrame:
        import baostock as bs

        day = self._latest_trade_date()
        rs = bs.query_all_stock(day=day)
        if str(getattr(rs, "error_code", "0")) != "0":
            logger.warning("query_all_stock 失败 code=%s msg=%s",
                           rs.error_code, rs.error_msg)
            return pd.DataFrame()
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=rs.fields)
        if "code" not in df.columns:
            return pd.DataFrame()
        df["symbol"] = df["code"].map(self._bs_code_to_internal)
        df["name"] = df.get("code_name", "")
        df = df[df["symbol"].astype(bool)].reset_index(drop=True)
        keep_cols = [c for c in ("symbol", "name", "tradeStatus") if c in df.columns]
        return df[keep_cols]

    def _fetch_bars_batch(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust_flag: str,
    ) -> pd.DataFrame:
        import baostock as bs

        results: list[pd.DataFrame] = []
        for sym in symbols:
            bs_code = self._internal_to_bs(sym)
            if not bs_code:
                continue
            try:
                rs = bs.query_history_k_data_plus(
                    code=bs_code,
                    fields=self._BARS_FIELDS,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag=adjust_flag,
                )
                if str(getattr(rs, "error_code", "0")) != "0":
                    logger.debug("baostock %s 拉日线 error: %s",
                                 sym, getattr(rs, "error_msg", ""))
                    continue
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=rs.fields)
                df["symbol"] = sym
                results.append(df)
            except Exception as exc:
                logger.warning("baostock 拉取 %s 日线失败: %s", sym, exc)
                continue
        if not results:
            return pd.DataFrame()
        df = pd.concat(results, ignore_index=True)
        df = df.rename(columns={"turn": "turnover_rate", "pctChg": "change_pct"})
        numeric_cols = (
            "open", "high", "low", "close", "preclose",
            "volume", "amount", "turnover_rate", "change_pct",
            "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM",
        )
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _fetch_index_cons(self, index_code: str) -> list[str]:
        import baostock as bs

        fn_map = {
            "hs300": bs.query_hs300_stocks,
            "zz500": bs.query_zz500_stocks,
            "sz50": bs.query_sz50_stocks,
        }
        fn = fn_map.get(index_code.lower())
        if fn is None:
            logger.warning("BaoStock 不支持的指数: %s", index_code)
            return []
        day = self._latest_trade_date()
        rs = fn(date=day)
        if str(getattr(rs, "error_code", "0")) != "0":
            logger.warning("query_%s_stocks 失败: %s",
                           index_code, getattr(rs, "error_msg", ""))
            return []
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return []
        df = pd.DataFrame(rows, columns=rs.fields)
        if "code" not in df.columns:
            return []
        return [s for s in (self._bs_code_to_internal(c) for c in df["code"]) if s]

    def _latest_trade_date(self) -> str:
        """近 14 天内最近一个交易日（YYYY-MM-DD）。"""
        import baostock as bs

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_trade_dates(start_date=start, end_date=end)
            if str(getattr(rs, "error_code", "0")) != "0":
                return end
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return end
            df = pd.DataFrame(rows, columns=rs.fields)
            if "is_trading_day" not in df.columns:
                return end
            traded = df[df["is_trading_day"] == "1"].sort_values("calendar_date")
            if traded.empty:
                return end
            return str(traded.iloc[-1]["calendar_date"])
        except Exception:
            return end

    # ── 代码格式互转 ───────────────────────────────────────────────────────

    @staticmethod
    def _internal_to_bs(symbol: str) -> str:
        """000001.SZ → sz.000001"""
        if "." not in symbol:
            return ""
        code, suffix = symbol.split(".", 1)
        suffix = suffix.upper()
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
        return f"{prefix}.{code}" if prefix else ""

    @staticmethod
    def _bs_code_to_internal(bs_code: object) -> str:
        """sh.600000 → 600000.SH"""
        s = str(bs_code or "").strip()
        if "." not in s:
            return ""
        prefix, code = s.split(".", 1)
        suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix.lower())
        return f"{code}.{suffix}" if suffix else ""
