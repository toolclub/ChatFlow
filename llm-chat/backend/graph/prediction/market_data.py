"""预测专用的加固行情客户端 — 重试退避 + provider 兜底 + 可区分的错误诊断。

为什么不直接用 quant.data_adapter：
  adapter.bars 的失败语义对预测不够用 —— 它把所有异常都吞成"返回空 DataFrame"
  （data_adapter.py:272-273），底层 provider 更是每只票都 try/except 成空 df
  （akshare_provider.py:352 / akshare_us_provider.py:161 / yfinance_us_provider.py:209-211）。
  compute_node 只能看到"行情为空"，无法告诉用户到底是 **雅虎 429 限流**、**DNS 不通**
  还是 **代码写错了**。用户明确要求"如果是网络原因也要表明"。

本模块的做法（只读复用 adapter/registry，绝不改它们 —— 智能选股共用）：
  1. 正常路径原样走 get_adapter().bars()（磁盘缓存 + registry fallback + 写盘）；
  2. 拿到空数据时，绕过 registry 逐个 provider 直连兜底（registry 把"返回空"当成功，
     不会 fallback 到下一个 provider —— 这正是美股 yfinance 被限流后拉空却没降级到
     akshare_us 的原因）；
  3. 全都失败 → 发一次轻量诊断探针（直连数据源 HTTP 接口看状态码），把失败原因归类成
     rate_limited / network / no_data / bad_symbol，抛出带中文可读 message 的
     MarketDataError；
  4. 可重试的 kind（限流/网络）走 3 次指数退避（1s/2s/4s）；no_data / bad_symbol 不重试。

preflight() 是 fetch_bars 的轻量版（只拉最近 ~10 个交易日 + 少量重试 + 硬超时），
用于图跑起来的第一步"2 秒判死"，避免 11~16 分钟资讯搜索之后才发现拉不到 K 线。

铁律：#5 DB/IO 全 async；#9 不吞异常 —— 每次重试/降级/兜底都 logger.warning 记原因。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import date, timedelta

import pandas as pd

from graph.prediction.config import PREDICTION_BARS_LOOKBACK_DAYS
from quant.data_adapter import get_adapter
from quant.domain import ProviderCapability
from quant.provider_registry import get_registry
from quant.providers.base import MarketDataProvider

logger = logging.getLogger("graph.prediction.market_data")


# ── 错误分类 ──────────────────────────────────────────────────────────────────

KIND_RATE_LIMITED = "rate_limited"
KIND_NETWORK = "network"
KIND_NO_DATA = "no_data"
KIND_BAD_SYMBOL = "bad_symbol"

ERROR_KINDS: frozenset[str] = frozenset(
    {KIND_RATE_LIMITED, KIND_NETWORK, KIND_NO_DATA, KIND_BAD_SYMBOL}
)

# 只有限流/网络值得重试；标的不存在 / 数据源明确没有这只票，重试多少次都一样
RETRYABLE_KINDS: frozenset[str] = frozenset({KIND_RATE_LIMITED, KIND_NETWORK})


# ── 可调参数 ──────────────────────────────────────────────────────────────────

FETCH_RETRY_TIMES: int = 3          # fetch_bars：初次 + 3 次重试（退避 1s/2s/4s）
FETCH_BACKOFF_BASE: float = 1.0
PROVIDER_CALL_TIMEOUT: float = 90.0  # 单个 provider 直连兜底的硬超时

PREFLIGHT_LOOKBACK_DAYS: int = 25    # ~10-15 个交易日，够判断"这只票能不能拉到数据"
PREFLIGHT_RETRY_TIMES: int = 1       # 预检要快：只给 1 次重试（1s 退避）
PREFLIGHT_TIMEOUT_SECONDS: float = float(
    os.environ.get("PREDICTION_PREFLIGHT_TIMEOUT", "30")
)

# 少于这个根数，ARIMA/GARCH 热身不够，与其让 engine 吐一条垃圾预测，不如诚实报错
MIN_BARS_FOR_FORECAST: int = 30

# 「能跑」和「跑得准」是两回事：30 根够 ARIMA/GARCH 不报错，但拟合质量很差。
# data_adapter 的 cover_ratio 只看"symbol 在不在缓存里"、不看缓存的日期区间
# （data_adapter.py:164-169），于是磁盘里只存了半年的票会以 coverage=100% 命中，
# 把 2 年的请求悄悄缩水（实测 000001.SZ 请求 730 天只回 124 根）。
# 低于这个根数就绕过缓存直连 provider 回源全量区间 —— 只是一只票，实测 ~2.6s，值。
# 注意它**不是**报错门槛：真的只有这么多历史的次新股，回源后拿不到更多也照跑
# （仍受 MIN_BARS_FOR_FORECAST 的硬门槛保护）。
PREFERRED_BARS_FOR_FORECAST: int = int(
    os.environ.get("PREDICTION_PREFERRED_BARS", "240")   # ≈ 1 年交易日
)

PROBE_TIMEOUT: float = 6.0
_PROBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class MarketDataError(Exception):
    """行情拉取失败 —— kind 区分限流 / 网络 / 无数据 / 代码错误。

    kind ∈ {"rate_limited", "network", "no_data", "bad_symbol"}
    message 是给终端用户看的中文文案（会直接进 SSE error 事件和 DB error 字段）。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else KIND_NO_DATA
        self.message = message

    def __repr__(self) -> str:  # 便于日志
        return f"MarketDataError(kind={self.kind!r}, message={self.message!r})"


def _message(
    kind: str, symbol: str, market: str, detail: str = "", *, malformed: bool = False,
) -> str:
    """按 kind 组装中文可读的错误文案（契约 C 规定的基础文案 + 诊断细节）。

    malformed 区分 bad_symbol 的两种成因 —— 别把"数据源里查无此票"说成"你格式写错了"，
    那会让用户对着一个格式完全正确的代码反复改。
    """
    if kind == KIND_RATE_LIMITED:
        source = "雅虎财经" if market == "us_stock" else "行情数据源"
        base = (
            f"数据源限流（{source} HTTP 429），请稍后重试；"
            "或在 .env 设置 QUANT_SCREENING_ENABLED=false 关闭智能选股预热以释放额度"
        )
    elif kind == KIND_NETWORK:
        base = "行情数据源网络不通（DNS 解析失败/连接超时），请检查网络"
    elif kind == KIND_BAD_SYMBOL and malformed:
        base = (
            f"标的代码格式不正确：{symbol}。"
            "A股请用 000001.SZ / 600000.SH，美股请用 MU / AAPL"
        )
    elif kind == KIND_BAD_SYMBOL:
        base = (
            f"数据源查无此标的：{symbol}，请确认代码是否正确"
            "（A股如 000001.SZ，美股如 MU）"
        )
    else:
        base = (
            "未找到该标的的历史行情，请确认代码是否正确"
            "（A股如 000001.SZ，美股如 MU）"
        )
    return f"{base}（诊断：{detail}）" if detail else base


# ── 证据嗅探 ─────────────────────────────────────────────────────────────────
# provider 层普遍把异常吞成空 DataFrame（akshare_provider.py:352 /
# akshare_us_provider.py:161 / yfinance_us_provider.py:209-211），所以光看抛出来的
# 异常常常一无所获。但它们**都会先 log 一行**再吞 —— 那行日志就是我们唯一的证据。
# 见 _capture_provider_logs()。

_RATE_LIMIT_PATTERNS = (
    "429", "too many requests", "rate limit", "ratelimit", "rate-limit",
    "yfratelimit", "限流", "频率", "每分钟", "调取过于频繁",
)
_NETWORK_PATTERNS = (
    "getaddrinfo", "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "dns", "connecterror", "connection refused",
    "connection reset", "connection aborted", "connection error", "remote end closed",
    "timeout", "timed out", "超时", "unreachable", "network is unreachable",
    "ssl", "proxy", "网络接收错误", "登录失败", "max retries exceeded",
)
# 数据源明确说"这只票不存在" —— 这是对**该标的**的确定性结论，优先级高于一切。
#
# 注意这里刻意 **不** 收录 yfinance 的 "possibly delisted; no timezone found"：
# 它在「真的没这只票」和「被 429 限流」两种情况下都会打，是出了名的误导性文案。
# 只认带 404 / "quote not found" 的硬证据 —— 限流时雅虎回的是 429，不会回 404。
_NOT_FOUND_PATTERNS = (
    "quote not found", "no data found for this date range",
    "no data found, symbol may be delisted", "404",
)


def _sniff_text(text: str) -> str | None:
    """从一段文本（异常 repr 或 provider 日志行）嗅探错误 kind；嗅不出返回 None。"""
    low = text.lower()
    # 顺序即优先级：标的不存在 > 限流 > 网络
    if any(p in low for p in _NOT_FOUND_PATTERNS):
        return KIND_BAD_SYMBOL
    if any(p in low for p in _RATE_LIMIT_PATTERNS):
        return KIND_RATE_LIMITED
    if any(p in low for p in _NETWORK_PATTERNS):
        return KIND_NETWORK
    return None


def _sniff(exc: BaseException) -> str | None:
    """从异常类型/文本嗅探错误 kind；嗅不出来返回 None（交给 HTTP 探针定夺）。"""
    kind = _sniff_text(f"{type(exc).__name__}: {exc}")
    if kind is not None:
        return kind
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return KIND_NETWORK
    return None


# ── provider 日志捕获 ─────────────────────────────────────────────────────────

# provider 吞异常前会 log 到这些 logger 上；不改 provider，只在拉取期间旁听。
_PROVIDER_LOGGERS = (
    "yfinance", "quant.akshare", "quant.akshare_us",
    "quant.baostock", "quant.tushare", "quant.registry",
)


class _LogCollector(logging.Handler):
    """把 provider 吞掉的错误日志收集成证据行。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:   # 铁律 #9：日志捕获自身出错也不能影响主流程，但要留痕
            logger.warning("provider 日志捕获失败 record=%r", getattr(record, "msg", "?"))


@contextlib.contextmanager
def _capture_provider_logs():
    """旁听 provider logger —— 它们把异常吞成空 df，但吞之前会 log 一行。

    这是把"provider 静默返回空"翻译成"到底为什么空"的唯一途径（不改 provider 的前提下）。
    """
    handler = _LogCollector()
    targets = [logging.getLogger(name) for name in _PROVIDER_LOGGERS]
    for lg in targets:
        lg.addHandler(handler)
    try:
        yield handler.lines
    finally:
        for lg in targets:
            try:
                lg.removeHandler(handler)
            except Exception as exc:
                logger.warning("移除日志捕获 handler 失败: %s", exc)


# ── 代码校验 / 归一化 ─────────────────────────────────────────────────────────

_CN_PLAIN = re.compile(r"^\d{6}$")
_CN_FULL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_US_TICKER = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}(\.[A-Z]{1,4})?$")


def _normalize_symbol(symbol: str, market: str) -> str:
    """校验并归一化标的代码；格式明显不对 → 立刻 bad_symbol（零网络开销）。"""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise MarketDataError(
            KIND_BAD_SYMBOL, _message(KIND_BAD_SYMBOL, symbol, market, malformed=True),
        )

    if market == "cn_a":
        if _CN_PLAIN.match(sym):
            from quant.providers.symbols import to_internal
            sym = to_internal(sym)          # 000001 → 000001.SZ（provider 要带后缀）
        if not _CN_FULL.match(sym):
            raise MarketDataError(
                KIND_BAD_SYMBOL,
                _message(KIND_BAD_SYMBOL, symbol, market, malformed=True),
            )
        return sym

    if not _US_TICKER.match(sym):
        raise MarketDataError(
            KIND_BAD_SYMBOL, _message(KIND_BAD_SYMBOL, symbol, market, malformed=True),
        )
    return sym


# ── provider 链（绕过 registry 的"空数据算成功"语义，逐个直连兜底） ────────────

def _provider_chain(market: str) -> list[MarketDataProvider]:
    """返回支持 (daily_bars, market) 的 provider，健康的排前面，DOWN 的垫底。

    健康度是启动时探活的旧结论，可能已过期 —— 所以 DOWN 的也留在链尾兜底，
    而不是像 registry.candidates() 那样直接剔除。
    """
    reg = get_registry()
    try:
        healthy = list(reg.candidates(ProviderCapability.DAILY_BARS, market=market))
    except Exception as exc:
        logger.warning("registry.candidates 异常（降级为全量扫描）: %s", exc)
        healthy = []

    seen = {p.name for p in healthy}
    degraded: list[MarketDataProvider] = []
    try:
        for info in reg.list_providers():
            if info.name in seen:
                continue
            if ProviderCapability.DAILY_BARS.value not in (info.capabilities or []):
                continue
            provider = reg.get_provider(info.name)
            if provider is None:
                continue
            if market not in getattr(provider, "supported_markets", {market}):
                continue
            degraded.append(provider)
    except Exception as exc:
        logger.warning("扫描 DOWN provider 异常（忽略，仅用健康 provider）: %s", exc)

    degraded.sort(key=lambda p: getattr(p, "priority", 999))
    chain = healthy + degraded
    if not chain:
        logger.warning("无任何 provider 支持 daily_bars(market=%s)", market)
    return chain


async def _provider_bars(
    provider: MarketDataProvider, symbol: str, start: str, end: str,
) -> pd.DataFrame:
    return await asyncio.wait_for(
        provider.daily_bars([symbol], start=start, end=end),
        timeout=PROVIDER_CALL_TIMEOUT,
    )


# ── HTTP 诊断探针（只在全部失败后跑一次，用来区分 429 / 网络 / 标的不存在） ────

async def _probe(symbol: str, market: str) -> tuple[str, str]:
    """直连数据源 HTTP 接口，从状态码/报文判定失败原因。

    provider 层把异常都吞成空 DataFrame，光看它们的返回值永远只能说"没数据"。
    这里花一次轻量请求（~6s 超时）换一个诚实的结论。
    """
    try:
        import httpx
    except ImportError as exc:
        logger.warning("httpx 不可用，无法诊断行情失败原因: %s", exc)
        return KIND_NO_DATA, "数据源不可用（诊断探针缺少 httpx）"

    if market == "us_stock":
        ticker = symbol.split(".")[0]
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"range": "5d", "interval": "1d"}
        source = "雅虎财经"
    else:
        code = symbol.split(".")[0]
        prefix = "1" if symbol.endswith(".SH") else "0"
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": f"{prefix}.{code}", "klt": "101", "fqt": "1",
            "fields1": "f1", "fields2": "f51,f53", "beg": "0",
            "end": "20500101", "lmt": "5",
        }
        source = "东方财富"

    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, headers={"User-Agent": _PROBE_UA},
        ) as client:
            resp = await client.get(url, params=params)
    except Exception as exc:
        kind = _sniff(exc) or KIND_NETWORK
        logger.warning("诊断探针请求 %s 失败 (%s): %s", source, kind, exc)
        return kind, f"{source}接口不可达（{type(exc).__name__}）"

    status = resp.status_code
    if status == 429:
        return KIND_RATE_LIMITED, f"{source}接口返回 HTTP 429"
    if status == 404:
        return KIND_BAD_SYMBOL, f"{source}接口返回 HTTP 404（标的不存在）"
    if status >= 500:
        return KIND_NETWORK, f"{source}接口返回 HTTP {status}（数据源服务异常）"
    if status != 200:
        return KIND_NO_DATA, f"{source}接口返回 HTTP {status}"

    try:
        payload = resp.json()
    except Exception as exc:
        logger.warning("诊断探针解析 %s 响应失败: %s", source, exc)
        return KIND_NO_DATA, f"{source}接口响应无法解析"

    if _probe_has_bars(payload, market):
        # 数据源自己是通的，但 provider 全线拉空 —— 多半是 SDK / 代码映射问题
        return KIND_NO_DATA, f"{source}接口可用但 provider 全部返回空数据"
    return KIND_NO_DATA, f"{source}接口未返回该标的的 K 线"


def _probe_has_bars(payload: object, market: str) -> bool:
    try:
        if market == "us_stock":
            result = (payload or {}).get("chart", {}).get("result") or []   # type: ignore[union-attr]
            return bool(result and (result[0].get("timestamp") or []))
        data = (payload or {}).get("data") or {}                            # type: ignore[union-attr]
        return bool(data.get("klines"))
    except Exception as exc:
        logger.warning("诊断探针判定 K 线存在性失败: %s", exc)
        return False


async def _diagnose(
    symbol: str, market: str,
    errors: list[BaseException], log_lines: list[str] | None = None,
) -> tuple[str, str]:
    """汇总所有证据判定失败原因，实在没证据才发 HTTP 探针。返回 (kind, detail)。

    证据来源有两类：
      ① 真抛出来的异常（tushare / baostock 会抛）
      ② provider 吞异常前打的日志行（akshare / yfinance 只会打日志然后返回空 df）

    优先级：bad_symbol > rate_limited > network > 探针。
    bad_symbol 排第一是因为它是对**这只票**的确定性结论；而探针打的是数据源的通用接口，
    在雅虎被 429 的环境里必然回 429 —— 若让探针说了算，一个根本不存在的 ticker 会被
    误报成"数据源限流，请稍后重试"，用户于是永远等不到正确答案。
    """
    evidence: list[tuple[str, str]] = []

    for exc in errors:
        kind = _sniff(exc)
        if kind is not None:
            evidence.append((kind, f"{type(exc).__name__}: {exc}"[:160]))

    for line in log_lines or []:
        kind = _sniff_text(line)
        if kind is not None:
            evidence.append((kind, line[:160]))

    for kind in (KIND_BAD_SYMBOL, KIND_RATE_LIMITED, KIND_NETWORK):
        for ev_kind, detail in evidence:
            if ev_kind == kind:
                logger.warning(
                    "行情失败诊断 symbol=%s kind=%s 依据=%s", symbol, kind, detail,
                )
                return kind, detail

    return await _probe(symbol, market)


# ── DataFrame 归一化（下游 forecast() 依赖：date 升序 ISO 字符串 + close 数值） ──

def _normalize_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """保持 provider 归一化列不变（symbol/date/open/high/low/close/volume/...），
    额外保证：只含目标标的、date 升序去重、OHLCV 数值化、close 非空。

    compute_node 用 .iloc[-1] 取最新收盘价，engine.forecast 用 close 序列 —— 都依赖升序。
    """
    work = df.copy()

    if "symbol" in work.columns:
        picked = work[work["symbol"].astype(str).str.upper() == symbol]
        if not picked.empty:
            work = picked
        else:
            logger.warning(
                "行情 symbol 列与请求不一致 (请求=%s, 返回=%s)，不做过滤",
                symbol, list(work["symbol"].astype(str).unique())[:3],
            )

    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work = work.dropna(subset=["date"])
        work = work.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        work["date"] = work["date"].dt.strftime("%Y-%m-%d")

    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    if "close" in work.columns:
        work = work[work["close"].notna()]

    return work.reset_index(drop=True)


# ── 单次拉取：adapter → 逐 provider 兜底 ──────────────────────────────────────

async def _fetch_once(
    symbol: str, market: str, start: str, end: str,
    errors: list[BaseException], log_lines: list[str],
    prefer_bars: int = 0,
) -> pd.DataFrame:
    # 全程旁听 provider 日志：它们把异常吞成空 df，日志是唯一能还原真实原因的证据。
    with _capture_provider_logs() as captured:
        try:
            df = await _fetch_once_inner(symbol, market, start, end, errors, prefer_bars)
        finally:
            log_lines.extend(captured)
    return df


async def _fetch_once_inner(
    symbol: str, market: str, start: str, end: str, errors: list[BaseException],
    prefer_bars: int = 0,
) -> pd.DataFrame:
    # adapter 命中缓存但根数不够时的最好结果 —— 直连兜底全挂了才退回它（有总比没有强）。
    best: pd.DataFrame = pd.DataFrame()

    # 1) 正常路径：磁盘缓存 + registry fallback + 写盘（不改 adapter，只读复用）
    try:
        df = await get_adapter().bars(
            symbols=[symbol], start=start, end=end, market=market, trace=[],
        )
        if df is not None and not df.empty:
            # data_adapter.py:164-169 的 cover_ratio 只看"symbol 在不在缓存里"，不看缓存
            # 覆盖的日期区间：磁盘里只有 28 天的票会以 coverage=100% 命中，把 730 天的请求
            # 悄悄缩水成 28 根。非空但不够热身 → 当成没拿到，落到直连兜底去回源全量区间。
            if prefer_bars <= 0 or len(df) >= prefer_bars:
                return df
            best = df
            logger.warning(
                "adapter.bars 命中缓存但根数不足 symbol=%s rows=%d < prefer=%d "
                "（缓存日期区间覆盖不足）→ 逐 provider 直连回源",
                symbol, len(df), prefer_bars,
            )
        else:
            logger.warning(
                "adapter.bars 返回空 symbol=%s market=%s → 逐 provider 直连兜底",
                symbol, market,
            )
    except Exception as exc:
        errors.append(exc)
        logger.warning("adapter.bars 异常 symbol=%s: %s", symbol, exc, exc_info=True)

    # 2) 兜底：registry 把"provider 返回空"当成功，不会自动降级 —— 这里手动逐个试。
    #    us_stock: yfinance_us 被 429 拉空 → akshare_us（东财，实测 200）
    #    cn_a:     akshare（sina DNS 挂）→ tushare → baostock
    for provider in _provider_chain(market):
        try:
            df = await _provider_bars(provider, symbol, start, end)
        except Exception as exc:
            errors.append(exc)
            logger.warning(
                "provider %s 拉取 %s 失败（继续兜底下一个）: %s",
                getattr(provider, "name", "?"), symbol, exc,
            )
            continue

        if df is not None and not df.empty:
            if prefer_bars <= 0 or len(df) >= prefer_bars:
                logger.info(
                    "provider %s 兜底成功 symbol=%s rows=%d",
                    getattr(provider, "name", "?"), symbol, len(df),
                )
                return df
            # 这个 provider 也只给了一小段 —— 留最长的那份，继续试下一个
            if len(df) > len(best):
                best = df
            logger.warning(
                "provider %s 根数不足 symbol=%s rows=%d < prefer=%d（继续兜底下一个）",
                getattr(provider, "name", "?"), symbol, len(df), prefer_bars,
            )
            continue
        logger.warning(
            "provider %s 返回空数据 symbol=%s（继续兜底下一个）",
            getattr(provider, "name", "?"), symbol,
        )

    # 全部 provider 都没给够 —— 退回见过的最长一份，由 fetch_bars 统一判 min_bars 并诚实报错
    return best


# ── 对外 API ──────────────────────────────────────────────────────────────────

async def fetch_bars(
    symbol: str,
    market: str = "cn_a",
    lookback_days: int = PREDICTION_BARS_LOOKBACK_DAYS,
    *,
    min_bars: int = MIN_BARS_FOR_FORECAST,
    prefer_bars: int | None = None,
    retry_times: int = FETCH_RETRY_TIMES,
    diag: dict | None = None,
) -> pd.DataFrame:
    """拉取日线 K 线；失败抛 MarketDataError（kind 可区分限流/网络/无数据/代码错）。

    返回的 DataFrame 与 provider 归一化列一致（symbol/date/open/high/low/close/volume/…），
    date 为 ISO 字符串且升序 —— compute_node / engine.forecast 直接可用。

    min_bars:    硬门槛。低于它宁可诚实报错，也不吐一条垃圾预测。
    prefer_bars: 回源门槛（默认 PREFERRED_BARS_FOR_FORECAST）。adapter 命中缓存但根数
                 低于它 → 绕过缓存直连 provider 回源全量区间（见常量处的注释）。
                 回源后仍不够也不报错 —— 只要过了 min_bars 就照跑。
    diag: 可选的 out-param，每轮把最新的 {"kind","detail"} 写进去。调用方在外层超时
          掐断时可以读它拿到"掐断那一刻已知的真实原因"，而不是笼统地报网络故障。
    """
    sym = _normalize_symbol(symbol, market)          # bad_symbol 立即抛，不发任何请求
    end_d = date.today()
    start_d = end_d - timedelta(days=max(int(lookback_days), 1))
    start, end = start_d.isoformat(), end_d.isoformat()

    prefer = (
        PREFERRED_BARS_FOR_FORECAST if prefer_bars is None else int(prefer_bars)
    )
    prefer = max(prefer, int(min_bars))   # 回源门槛不可能低于硬门槛

    errors: list[BaseException] = []
    log_lines: list[str] = []
    kind, detail = KIND_NO_DATA, ""

    for attempt in range(retry_times + 1):
        if attempt:
            delay = FETCH_BACKOFF_BASE * (2 ** (attempt - 1))   # 1s / 2s / 4s
            logger.warning(
                "fetch_bars 第 %d/%d 次重试 symbol=%s kind=%s（%.0fs 后）: %s",
                attempt, retry_times, sym, kind, delay, detail,
            )
            await asyncio.sleep(delay)

        errors.clear()
        log_lines.clear()
        df = await _fetch_once(sym, market, start, end, errors, log_lines, prefer)

        if df is not None and not df.empty:
            bars = _normalize_bars(df, sym)
            if min_bars > 0 and len(bars) < min_bars:
                # 拿到了数据但根数不够热身 —— 不重试（再拉一次也是这么多），诚实报错
                logger.warning(
                    "fetch_bars 行情根数不足 symbol=%s bars=%d < min=%d",
                    sym, len(bars), min_bars,
                )
                raise MarketDataError(
                    KIND_NO_DATA,
                    _message(
                        KIND_NO_DATA, sym, market,
                        f"仅取到 {len(bars)} 根 K 线，预测至少需要 {min_bars} 根",
                    ),
                )
            _warn_if_short_history(bars, sym, market, start, lookback_days)
            logger.info(
                "fetch_bars 成功 symbol=%s market=%s bars=%d (%s → %s)",
                sym, market, len(bars),
                bars["date"].iloc[0] if "date" in bars.columns and len(bars) else "?",
                bars["date"].iloc[-1] if "date" in bars.columns and len(bars) else "?",
            )
            return bars

        kind, detail = await _diagnose(sym, market, errors, log_lines)
        if diag is not None:
            diag["kind"], diag["detail"] = kind, detail

        if kind not in RETRYABLE_KINDS:
            logger.warning(
                "fetch_bars 失败且不可重试 symbol=%s kind=%s: %s", sym, kind, detail,
            )
            raise MarketDataError(kind, _message(kind, sym, market, detail))

    logger.warning(
        "fetch_bars 重试 %d 次仍失败 symbol=%s kind=%s: %s",
        retry_times, sym, kind, detail,
    )
    raise MarketDataError(kind, _message(kind, sym, market, detail))


def _warn_if_short_history(
    bars: pd.DataFrame, symbol: str, market: str, start: str, lookback_days: int,
) -> None:
    """请求了 N 天历史却只拿到很短一段 —— 记一条告警（不报错，够 min_bars 就能预测）。

    已知成因（不在本模块，别在这儿修）：quant/data_adapter.py:164-169 的 cover_ratio
    只统计"请求的 symbol 在缓存里出没出现"，完全不看缓存覆盖的**日期区间**。于是
    磁盘里只缓存了半年的票，会以 coverage=100% 命中，把 2 年的请求悄悄缩水成半年。
    实测：000001.SZ 请求 730 天 → 只回 124 根（2026-01-05 起）；MU 未受影响（缓存是全的）。
    """
    if "date" in bars.columns and len(bars):
        first = str(bars["date"].iloc[0])
        if first > start:
            span_days = (date.fromisoformat(str(bars["date"].iloc[-1]))
                         - date.fromisoformat(first)).days
            if span_days < lookback_days * 0.6:
                logger.warning(
                    "行情历史短于预期 symbol=%s market=%s 请求 %d 天(自 %s) 实得 %d 根/%d 天"
                    "(自 %s) — 疑似 data_adapter 缓存区间覆盖不足，预测精度可能下降",
                    symbol, market, lookback_days, start, len(bars), span_days, first,
                )


async def preflight(symbol: str, market: str = "cn_a") -> dict:
    """开跑前的行情预检 —— 目标是"判死要快"，别让用户等完 12 维资讯再看到红错。

    ★ 必须按 compute 的**真实门槛**（MIN_BARS_FOR_FORECAST）预检，而不是"拉不拉得到"。
      否则只有 28 根缓存的票会以 bars=17 通过预检，跑完好几分钟资讯搜索，才在 compute
      里撞上"仅取到 28 根 K 线，至少需要 30 根"—— 那正是 v1 修掉的 fail-slow，不能从
      这扇门再溜回来。预检就拉 compute 要用的同一段区间（命中缓存时是亚秒级；
      缓存不足时正好在这里就触发直连回源，compute 再拉就是热的）。
    成功 → {"ok": True, "bars": int, "last_close": float, ...}；失败 → raise MarketDataError。
    """
    sym = _normalize_symbol(symbol, market)          # 代码格式不对 → 零网络开销直接抛
    diag: dict = {}

    try:
        bars = await asyncio.wait_for(
            fetch_bars(
                sym, market, PREDICTION_BARS_LOOKBACK_DAYS,
                min_bars=MIN_BARS_FOR_FORECAST,      # 与 compute 同一门槛 → 判死要早
                retry_times=PREFLIGHT_RETRY_TIMES,
                diag=diag,
            ),
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except MarketDataError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        # 掐断前如果已经诊断出真实原因（比如 429），就报它 —— 别把限流说成"网络不通"，
        # 那会把用户支到错误的排查方向上去。
        kind = diag.get("kind") or KIND_NETWORK
        detail = diag.get("detail") or "数据源无响应"
        logger.warning(
            "preflight 超时 symbol=%s market=%s (>%.0fs) kind=%s: %s",
            sym, market, PREFLIGHT_TIMEOUT_SECONDS, kind, detail,
        )
        raise MarketDataError(
            kind,
            _message(
                kind, sym, market,
                f"行情预检超时（>{PREFLIGHT_TIMEOUT_SECONDS:.0f}s）：{detail}",
            ),
        ) from exc
    except Exception as exc:
        # 铁律 #9：兜底也要落日志，不能把未知异常吞掉或原样抛给图
        logger.warning(
            "preflight 未预期异常 symbol=%s: %s", sym, exc, exc_info=True,
        )
        raise MarketDataError(
            KIND_NO_DATA,
            _message(KIND_NO_DATA, sym, market, f"{type(exc).__name__}: {exc}"[:120]),
        ) from exc

    last_close = None
    last_date = ""
    try:
        if "close" in bars.columns and len(bars):
            last_close = float(bars["close"].iloc[-1])
        if "date" in bars.columns and len(bars):
            last_date = str(bars["date"].iloc[-1])
    except Exception as exc:
        logger.warning("preflight 读取最新收盘价失败（不影响预检结论）: %s", exc)

    result = {
        "ok": True,
        "symbol": sym,
        "market": market,
        "bars": int(len(bars)),
        "last_close": last_close,
        "last_date": last_date,
    }
    logger.info("preflight 通过 %s", result)
    return result


__all__ = [
    "MarketDataError",
    "fetch_bars",
    "preflight",
    "ERROR_KINDS",
    "RETRYABLE_KINDS",
    "KIND_RATE_LIMITED",
    "KIND_NETWORK",
    "KIND_NO_DATA",
    "KIND_BAD_SYMBOL",
    "MIN_BARS_FOR_FORECAST",
    "PREFLIGHT_TIMEOUT_SECONDS",
]
