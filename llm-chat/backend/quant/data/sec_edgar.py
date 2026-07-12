"""美股 SEC EDGAR 数据源 — CIK 映射 / submissions / 8-K Item 语义 / XBRL 减值数值。

设计文档 §1.2 的数据层落地。美股这条线的数据质量**明显高于 A 股**：
  - 8-K 的 `items` 字段**直接给 Item 编号**，无需解析正文（MU 实测 items='2.02,9.01'）
    → Item 4.02「财报不可靠」= 强负面信号，规则可直接定死 -1.0
  - NT 10-K / NT 10-Q（延迟申报）是爆雷前兆，form 字段直接可见
  - XBRL companyfacts 直接给 `GoodwillImpairmentLoss` 数值（A 股完全拿不到）

★★ 两个硬门槛（实测，不是猜的）★★
  1. **必须带合规 User-Agent**。默认 UA / `python-httpx/*` / `curl/*` 一律 **403**
     （实测：不带 UA → 403；带 "ChatFlow/1.0 (contact: ...)" → 200）。
     可用 SEC_USER_AGENT 环境变量覆盖联系方式 —— SEC 的公平使用政策要求能联系到你。
  2. **限流**。官方声明上限 10 req/s，本模块自限 6 req/s（令牌桶）+ 429 指数退避。
     把 SEC 打到封 IP，整个美股预测线就废了，不值得。

局限（诚实标注，UI 要写）：
  Form 4 的**买卖方向/股数/价格不在 submissions.json 里**（要逐条下 XML 解析）。
  本模块**只用「近 N 天 Form 4 / 144 条数」作为内部人减持压力的代理指标，不做金额级量化**。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, timedelta

logger = logging.getLogger("quant.data.sec_edgar")


# ── 合规配置 ──────────────────────────────────────────────────────────────────

SEC_CONTACT: str = os.environ.get("SEC_CONTACT_EMAIL", "chatflow-bot@example.com")
SEC_USER_AGENT: str = os.environ.get(
    "SEC_USER_AGENT", f"ChatFlow/2.0 (contact: {SEC_CONTACT})",
)
_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

SEC_RPS: float = float(os.environ.get("SEC_MAX_RPS", "6"))   # 官方上限 10，自限 6
SEC_TIMEOUT: float = 30.0
SEC_RETRY_TIMES: int = 2                                      # 初次 + 2 次重试（1s / 2s）
SEC_BACKOFF_BASE: float = 1.0

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"

TICKERS_TTL: float = 86400.0     # ticker→CIK 映射一天刷一次（9304 条，很稳）
SUBMISSIONS_TTL: float = 900.0   # 15 分钟
FACTS_TTL: float = 3600.0        # companyfacts 4MB / 2.6s，缓存 1 小时

DEFAULT_MONTHS: int = 24
INSIDER_WINDOW_DAYS: int = 90    # Form 4 / 144 的「近 90 天条数」代理指标窗口


class SecEdgarError(Exception):
    """SEC EDGAR 不可用（403 UA 不合规 / 429 限流 / 网络）。重试耗尽后抛出。

    与「这家公司没有该类 filing」严格区分 —— 后者返回空列表。
    """

    def __init__(self, source: str, message: str) -> None:
        super().__init__(message)
        self.source = source
        self.message = message


# ── 8-K Item 语义表（无需解析正文，submissions.json 的 items 字段直接给编号）────

ITEM_8K: dict[str, tuple[str, float]] = {
    # item: (中文名, severity ∈ [-1, 0])
    "1.01": ("签订重大协议", 0.0),
    "1.02": ("终止重大协议", -0.3),
    "1.03": ("破产或托管", -1.0),
    "1.05": ("重大网络安全事件", -0.6),
    "2.02": ("业绩发布", 0.0),          # 中性：好坏要看内容，不在 items 里
    "2.03": ("新增重大债务", -0.2),
    "2.04": ("触发债务加速到期", -0.7),
    "2.05": ("退出/处置business 计提成本", -0.4),
    "2.06": ("重大资产减值", -0.8),     # ← 美股能拿到的「商誉爆雷」硬信号
    "3.01": ("退市通知/不满足上市规则", -0.9),
    "3.03": ("证券持有人权利重大变更", -0.3),
    "4.01": ("更换会计师事务所", -0.5),
    "4.02": ("前期财报不可依赖", -1.0),  # ★★ 最强负面：财务造假/重述的官方标记
    "5.02": ("董事/高管变动", -0.2),
    "5.03": ("章程修订", 0.0),
    "5.07": ("股东投票结果", 0.0),
    "7.01": ("Reg FD 披露", 0.0),
    "8.01": ("其他事项", 0.0),
    "9.01": ("财务报表与附件", 0.0),
}

# 延迟申报 —— 爆雷前兆（"我们赶不出财报"通常意味着审计出了问题）
LATE_FILING_FORMS: dict[str, float] = {
    "NT 10-K": -0.7,
    "NT 10-Q": -0.6,
    "NT 20-F": -0.7,
    "NT 11-K": -0.3,
}
DELISTING_FORMS: dict[str, float] = {"25": -0.9, "25-NSE": -0.9}

PLACEMENT_FORMS: frozenset[str] = frozenset({
    "S-3", "S-3ASR", "S-3/A", "S-1", "S-1/A",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7",
})
INSIDER_FORMS: frozenset[str] = frozenset({"4", "4/A"})
PROPOSED_SALE_FORMS: frozenset[str] = frozenset({"144", "144/A"})
# ★ SEC 对同一类表单存在两种写法（MU 实测同时出现 "SC 13G/A" 与 "SCHEDULE 13G/A"），
#   只收前者会漏掉一半大股东持仓变动。
BLOCKHOLDER_FORMS: frozenset[str] = frozenset({
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
    "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
})

# XBRL 减值标签（us-gaap）。MU 实测 GoodwillImpairmentLoss / AssetImpairmentCharges 都在。
IMPAIRMENT_TAGS: tuple[str, ...] = (
    "GoodwillImpairmentLoss",
    "AssetImpairmentCharges",
    "ImpairmentOfIntangibleAssetsExcludingGoodwill",
    "GoodwillAndIntangibleAssetImpairment",
    "ImpairmentOfLongLivedAssetsHeldAndUsed",
)

EVENT_NEGATIVE = "negative"
EVENT_PLACEMENT = "private_placement"
EVENT_REDUCTION = "stake_reduction"

CONF_HARD = "hard"
CONF_INFERRED = "inferred"


# ── 令牌桶限流（6 req/s，全进程共享）───────────────────────────────────────────

class _RateLimiter:
    """最朴素的令牌桶：保证任意相邻两次请求间隔 >= 1/rps。

    不用 Semaphore —— 那限的是并发数，不是速率。SEC 卡的是速率。
    """

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / max(rps, 0.5)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


_limiter = _RateLimiter(SEC_RPS)


# ── HTTP ─────────────────────────────────────────────────────────────────────

_JSON_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _CACHE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CACHE_LOCKS[key] = lock
    return lock


async def _get_json(url: str, *, ttl: float, source: str) -> dict:
    """带 TTL 缓存 + 单飞锁 + 限流 + 429 退避的 GET。失败抛 SecEdgarError。"""
    now = time.time()
    hit = _JSON_CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]

    async with _lock_for(url):
        hit = _JSON_CACHE.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

        try:
            import httpx
        except ImportError as exc:
            logger.warning("httpx 不可用，无法访问 SEC EDGAR: %s", exc)
            raise SecEdgarError(source, f"httpx 未安装：{exc}") from exc

        last: str = ""
        for attempt in range(SEC_RETRY_TIMES + 1):
            if attempt:
                delay = SEC_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "SEC %s 第 %d/%d 次重试（%.0fs 后）: %s",
                    url, attempt, SEC_RETRY_TIMES, delay, last,
                )
                await asyncio.sleep(delay)

            await _limiter.acquire()
            try:
                async with httpx.AsyncClient(
                    timeout=SEC_TIMEOUT, headers=_HEADERS, follow_redirects=True,
                ) as client:
                    resp = await client.get(url)
            except Exception as exc:      # noqa: BLE001 — 网络异常，退避重试
                last = f"{type(exc).__name__}: {exc}"
                logger.warning("SEC 请求失败 %s: %s", url, last)
                continue

            if resp.status_code == 403:
                # UA 不合规是**配置错误**，重试一万次也没用 —— 立刻抛，别浪费时间
                logger.warning(
                    "SEC 返回 403（User-Agent 不合规）UA=%r url=%s", SEC_USER_AGENT, url,
                )
                raise SecEdgarError(
                    source,
                    "SEC EDGAR 拒绝访问（HTTP 403）：User-Agent 不合规。"
                    "请在 .env 设置 SEC_CONTACT_EMAIL 为真实联系邮箱",
                )
            if resp.status_code == 404:
                logger.warning("SEC 404（无此 CIK / 无此资源）url=%s", url)
                raise SecEdgarError(source, f"SEC EDGAR 查无此资源（HTTP 404）：{url}")
            if resp.status_code == 429:
                last = "HTTP 429（限流）"
                logger.warning("SEC 限流 429 url=%s（退避重试）", url)
                continue
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code}"
                logger.warning("SEC 返回 %d url=%s", resp.status_code, url)
                continue

            try:
                payload = resp.json()
            except Exception as exc:      # noqa: BLE001
                last = f"响应无法解析为 JSON: {exc}"
                logger.warning("SEC 响应解析失败 url=%s: %s", url, exc)
                continue

            _JSON_CACHE[url] = (time.time(), payload)
            return payload

        raise SecEdgarError(source, f"SEC EDGAR 请求失败（重试 {SEC_RETRY_TIMES} 次）：{last}")


def clear_cache() -> None:
    """清空进程内 SEC 响应缓存（测试用）。"""
    _JSON_CACHE.clear()


# ── CIK 映射 ──────────────────────────────────────────────────────────────────

async def resolve_cik(ticker: str) -> str | None:
    """ticker → 10 位补零 CIK（MU → '0000723125'）。查不到返回 None（不抛）。"""
    sym = str(ticker or "").strip().upper().split(".")[0]
    if not sym:
        return None

    payload = await _get_json(TICKERS_URL, ttl=TICKERS_TTL, source="sec_tickers")
    for row in payload.values():
        if str(row.get("ticker", "")).upper() == sym:
            return str(row.get("cik_str", "")).zfill(10)

    logger.warning("SEC company_tickers.json 里查无 ticker=%s（%d 条）", sym, len(payload))
    return None


# ── submissions ───────────────────────────────────────────────────────────────

def _iso(value: object) -> str:
    return str(value or "")[:10]


def _cutoff(months: int) -> str:
    if months <= 0:
        return ""
    return (date.today() - timedelta(days=int(months) * 31)).isoformat()


def _filing_url(cik: str, accession: str, doc: str) -> str:
    if not accession:
        return ""
    acc = accession.replace("-", "")
    cik_int = str(int(cik))
    if not doc:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/"
    return ARCHIVE_URL.format(cik_int=cik_int, acc=acc, doc=doc)


def _rows_to_filings(cik: str, name: str, ticker: str, block: dict) -> list[dict]:
    """把 submissions 的「列式」结构（每个字段一个等长数组）转成行式 dict。"""
    forms = block.get("form") or []
    out: list[dict] = []
    for i, form in enumerate(forms):
        def _at(key: str) -> str:
            seq = block.get(key) or []
            return str(seq[i]) if i < len(seq) and seq[i] is not None else ""

        accession = _at("accessionNumber")
        doc = _at("primaryDocument")
        items_raw = _at("items")
        items = [s.strip() for s in items_raw.split(",") if s.strip()] if items_raw else []
        out.append({
            "symbol": ticker,
            "market": "us_stock",
            "cik": cik,
            "company": name,
            "form": str(form),
            "filing_date": _iso(_at("filingDate")),
            "report_date": _iso(_at("reportDate")),
            "accession": accession,
            "items": items,
            "item_labels": [
                f"{it} {ITEM_8K[it][0]}" for it in items if it in ITEM_8K
            ],
            "primary_doc": doc,
            "description": _at("primaryDocDescription"),
            "url": _filing_url(cik, accession, doc),
            "source": "sec",
        })
    return out


async def fetch_submissions(
    ticker: str,
    *,
    months: int = DEFAULT_MONTHS,
    forms: frozenset[str] | set[str] | None = None,
    include_history: bool = False,
) -> list[dict]:
    """全部 SEC 备案列表，按 filing_date 倒序。

    Args:
        months:          回溯月数；0 = 不限（配合 include_history 可拿到 1994 年至今全量）
        forms:           只要这些 form（如 {"8-K"}）；None = 全部
        include_history: True 时把 `filings.files` 里的历史分页也拉进来
                         （MU: recent 1001 条 + 历史 1757 条 = 2758 条）

    Returns（每条）:
        {"symbol","market","cik","company","form","filing_date","report_date",
         "accession","items":[...],"item_labels":[...],"primary_doc","description",
         "url","source":"sec"}
    """
    cik = await resolve_cik(ticker)
    if not cik:
        return []

    payload = await _get_json(
        SUBMISSIONS_URL.format(cik=cik), ttl=SUBMISSIONS_TTL, source="sec_submissions",
    )
    name = str(payload.get("name", "") or "")
    sym = str(ticker).strip().upper().split(".")[0]
    filings = payload.get("filings") or {}

    rows = _rows_to_filings(cik, name, sym, filings.get("recent") or {})

    if include_history:
        for page in filings.get("files") or []:
            fname = str(page.get("name", "") or "")
            if not fname:
                continue
            try:
                extra = await _get_json(
                    f"https://data.sec.gov/submissions/{fname}",
                    ttl=SUBMISSIONS_TTL, source="sec_submissions",
                )
            except SecEdgarError as exc:
                # 历史分页失败只降级（recent 已经拿到了），别让 2017 年前的数据拖垮本次预测
                logger.warning("SEC 历史分页 %s 拉取失败（降级：仅用 recent）: %s", fname, exc)
                continue
            rows.extend(_rows_to_filings(cik, name, sym, extra))

    cutoff = _cutoff(months)
    if cutoff:
        rows = [r for r in rows if not r["filing_date"] or r["filing_date"] >= cutoff]
    if forms:
        wanted = {f.upper() for f in forms}
        rows = [r for r in rows if r["form"].upper() in wanted]

    rows.sort(key=lambda r: r["filing_date"], reverse=True)
    logger.info(
        "fetch_submissions %s(CIK %s) → %d 条 months=%s forms=%s history=%s",
        sym, cik, len(rows), months, sorted(forms) if forms else "all", include_history,
    )
    return rows


# ── 8-K Item 语义 ─────────────────────────────────────────────────────────────

async def fetch_8k_items(ticker: str, *, months: int = DEFAULT_MONTHS) -> list[dict]:
    """8-K 列表 + 逐条 Item 语义解析（不下载正文）。

    Returns（每条在 fetch_submissions 的基础上多）:
        {"severity": float,        # 该 8-K 所有 item 里最负的一个
         "negative_items": [{"item","label","severity"}],
         "confidence": "hard"}     # Item 编号是 SEC 的官方分类，不是我们猜的
    """
    filings = await fetch_submissions(ticker, months=months, forms={"8-K", "8-K/A"})
    out: list[dict] = []
    for f in filings:
        negatives = [
            {"item": it, "label": ITEM_8K[it][0], "severity": ITEM_8K[it][1]}
            for it in f["items"]
            if it in ITEM_8K and ITEM_8K[it][1] < 0
        ]
        severity = min((n["severity"] for n in negatives), default=0.0)
        out.append({
            **f,
            "severity": severity,
            "negative_items": negatives,
            "unknown_items": [it for it in f["items"] if it not in ITEM_8K],
            "confidence": CONF_HARD,
        })
    return out


# ── XBRL ─────────────────────────────────────────────────────────────────────

async def fetch_company_facts(
    ticker: str, *, tags: tuple[str, ...] = IMPAIRMENT_TAGS,
) -> dict[str, list[dict]]:
    """XBRL companyfacts 里指定 us-gaap 标签的时间序列（4MB / 2.6s，带 1h 缓存）。

    Returns: {tag: [{"end","start","val","form","filed","fy","fp","accn"}, ...]}
    """
    cik = await resolve_cik(ticker)
    if not cik:
        return {}

    payload = await _get_json(
        COMPANYFACTS_URL.format(cik=cik), ttl=FACTS_TTL, source="sec_xbrl",
    )
    us_gaap = (payload.get("facts") or {}).get("us-gaap") or {}

    out: dict[str, list[dict]] = {}
    for tag in tags:
        node = us_gaap.get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        usd = units.get("USD") or next(iter(units.values()), [])
        series = [
            {
                "end": _iso(p.get("end")),
                "start": _iso(p.get("start")),
                "val": p.get("val"),
                "form": str(p.get("form", "") or ""),
                "filed": _iso(p.get("filed")),
                "fy": p.get("fy"),
                "fp": str(p.get("fp", "") or ""),
                "accn": str(p.get("accn", "") or ""),
            }
            for p in usd
        ]
        series.sort(key=lambda p: p["end"])
        out[tag] = series
    return out


async def fetch_impairments(
    ticker: str, *, months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """窗口内**非零**的资产/商誉减值（XBRL 硬数值 —— A 股完全拿不到的东西）。"""
    cutoff = _cutoff(months)
    try:
        facts = await fetch_company_facts(ticker)
    except SecEdgarError as exc:
        logger.warning("XBRL companyfacts 拉取失败（降级：跳过减值维度）%s: %s", ticker, exc)
        return []

    out: list[dict] = []
    for tag, series in facts.items():
        for p in series:
            val = p.get("val")
            if not val:                              # 0 或 None 都不是减值事件
                continue
            filed = p.get("filed") or p.get("end") or ""
            if cutoff and filed and filed < cutoff:
                continue
            out.append({
                "symbol": str(ticker).upper(),
                "market": "us_stock",
                "event_type": EVENT_NEGATIVE,
                "event_subtype": "impairment",
                "severity": -0.8,
                "needs_llm": False,
                "confidence": CONF_HARD,             # ★ XBRL 数值，精确到美元
                "reason": f"XBRL {tag} = {val:,.0f} USD（{p.get('form')} {p.get('fy')}{p.get('fp')}）",
                "title": f"{tag} {val:,.0f} USD",
                "ann_type": tag,
                "ann_date": filed,
                "url": "",
                "source": "sec_xbrl",
                "tag": tag,
                "value": val,
                "period_end": p.get("end"),
            })
    out.sort(key=lambda x: x["ann_date"], reverse=True)
    return out


# ── 与 A 股同构的四个公开 API（announcement.py 按 market 分流到这里）──────────

async def fetch_negative_events(
    ticker: str, *, months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """美股负面事件：8-K 负面 Item + 延迟申报(NT 10-K/Q) + 退市通知 + XBRL 减值。

    返回结构与 announcement.fetch_negative_events 一致（下游 analyze/engine 不必分市场）。
    """
    events: list[dict] = []

    # ① 8-K 负面 Item（主源）
    for f in await fetch_8k_items(ticker, months=months):
        if not f["negative_items"]:
            continue
        labels = "、".join(f"Item {n['item']} {n['label']}" for n in f["negative_items"])
        events.append({
            "symbol": f["symbol"], "market": "us_stock",
            "event_type": EVENT_NEGATIVE,
            "event_subtype": "8k_item",
            "severity": f["severity"],
            "needs_llm": False,                      # Item 编号是 SEC 官方分类，无需 LLM 猜
            "confidence": CONF_HARD,
            "reason": f"8-K {labels}（SEC 官方 Item 分类，规则定级 {f['severity']}）",
            "title": f"8-K: {labels}",
            "ann_type": "8-K",
            "ann_date": f["filing_date"],
            "url": f["url"],
            "source": "sec",
            "items": f["items"],
        })

    # ② 延迟申报 / 退市通知（副源，失败降级）
    late_forms = set(LATE_FILING_FORMS) | set(DELISTING_FORMS)
    try:
        for f in await fetch_submissions(ticker, months=months, forms=late_forms):
            sev = LATE_FILING_FORMS.get(f["form"]) or DELISTING_FORMS.get(f["form"]) or -0.5
            kind = "延迟申报（财报难产，爆雷前兆）" if f["form"] in LATE_FILING_FORMS else "退市通知"
            events.append({
                "symbol": f["symbol"], "market": "us_stock",
                "event_type": EVENT_NEGATIVE,
                "event_subtype": "late_filing" if f["form"] in LATE_FILING_FORMS else "delisting",
                "severity": sev,
                "needs_llm": False,
                "confidence": CONF_HARD,
                "reason": f"SEC form {f['form']} —— {kind}（规则定级 {sev}）",
                "title": f"{f['form']}: {kind}",
                "ann_type": f["form"],
                "ann_date": f["filing_date"],
                "url": f["url"],
                "source": "sec",
            })
    except SecEdgarError as exc:
        logger.warning("延迟申报/退市 form 拉取失败（降级）%s: %s", ticker, exc)

    # ③ XBRL 减值（副源，内部已降级）
    events.extend(await fetch_impairments(ticker, months=months))

    events.sort(key=lambda x: x.get("ann_date") or "", reverse=True)
    logger.info("sec.fetch_negative_events %s → %d 条", str(ticker).upper(), len(events))
    return events


async def fetch_placements(ticker: str, *, months: int = DEFAULT_MONTHS) -> list[dict]:
    """美股增发：form ∈ S-3 / S-3ASR / 424B*（注册声明 + 招股补充）。

    ★ 只有 form 类型，**没有发行价/发行数**（要解析正文）→ confidence=inferred，
      discount_rate/dilution_ratio 一律 None，engine 用条数做代理。
    """
    filings = await fetch_submissions(ticker, months=months, forms=PLACEMENT_FORMS)
    out: list[dict] = []
    for f in filings:
        out.append({
            "symbol": f["symbol"], "market": "us_stock",
            "event_type": EVENT_PLACEMENT,
            "event_subtype": "placement",
            "source": "sec",
            "confidence": CONF_INFERRED,
            "issue_method": f["form"],
            "shares": None, "price": None, "latest_price": None,
            "discount_rate": None, "dilution_ratio": None, "lockup": "",
            "issue_date": f["filing_date"], "listing_date": "",
            "ann_date": f["filing_date"],
            "title": f"{f['form']}: {f['description'] or '增发/注册声明'}",
            "ann_type": f["form"],
            "url": f["url"],
            "title_hash": "",
            "severity": None,
            "needs_llm": False,
            "reason": f"SEC form {f['form']}（增发注册声明/招股补充；未解析正文，无发行价）",
        })
    logger.info("sec.fetch_placements %s → %d 条", str(ticker).upper(), len(out))
    return out


async def fetch_reductions(ticker: str, *, months: int = DEFAULT_MONTHS) -> list[dict]:
    """美股内部人减持代理指标：form 4（内部人交易）+ form 144（拟售限制性证券）。

    ★★ 诚实声明（文档 §1.2「局限」）：submissions.json **不含买卖方向/股数/价格**，
       要逐条下 XML 才拿得到。本函数**只统计条数**，返回一条汇总记录 +
       近 90 天的明细列表（每条只有日期和 form）。**不做金额级量化**。
    """
    sym = str(ticker).strip().upper().split(".")[0]
    filings = await fetch_submissions(
        ticker, months=months, forms=INSIDER_FORMS | PROPOSED_SALE_FORMS,
    )
    if not filings:
        return []

    window_start = (date.today() - timedelta(days=INSIDER_WINDOW_DAYS)).isoformat()
    recent = [f for f in filings if f["filing_date"] >= window_start]
    n_form4 = sum(1 for f in recent if f["form"].upper() in INSIDER_FORMS)
    n_144 = sum(1 for f in recent if f["form"].upper() in PROPOSED_SALE_FORMS)

    out: list[dict] = [{
        "symbol": sym, "market": "us_stock",
        "event_type": EVENT_REDUCTION,
        "event_subtype": "insider_activity_summary",
        "source": "sec",
        "confidence": CONF_INFERRED,          # ★ 条数代理，不是金额
        "holder": "", "direction": "",
        "shares_wan": None, "pct_of_total": None, "pct_of_float": None,
        "after_shares_wan": None, "after_pct_of_total": None,
        "start_date": window_start, "end_date": date.today().isoformat(),
        "ann_date": recent[0]["filing_date"] if recent else filings[0]["filing_date"],
        "title": f"近 {INSIDER_WINDOW_DAYS} 天内部人申报 Form 4 × {n_form4}、Form 144 × {n_144}",
        "ann_type": "Form 4 / 144",
        "url": "",
        "title_hash": "",
        "severity": None,
        "needs_llm": False,
        "form4_count_90d": n_form4,
        "form144_count_90d": n_144,
        "total_count_window": len(filings),
        "reason": (
            f"SEC Form 4/144 条数代理指标（近 {INSIDER_WINDOW_DAYS} 天 {n_form4}+{n_144} 笔）。"
            "submissions.json 不含买卖方向/股数/价格，**未解析金额**"
        ),
    }]

    for f in recent[:50]:
        out.append({
            "symbol": sym, "market": "us_stock",
            "event_type": EVENT_REDUCTION,
            "event_subtype": "insider_filing",
            "source": "sec",
            "confidence": CONF_INFERRED,
            "holder": "", "direction": "",
            "shares_wan": None, "pct_of_total": None, "pct_of_float": None,
            "after_shares_wan": None, "after_pct_of_total": None,
            "start_date": "", "end_date": "",
            "ann_date": f["filing_date"],
            "title": f"Form {f['form']} 内部人申报",
            "ann_type": f["form"],
            "url": f["url"],
            "title_hash": "",
            "severity": None,
            "needs_llm": False,
            "reason": "SEC 内部人申报（未解析 XML，无金额/方向）",
        })

    logger.info(
        "sec.fetch_reductions %s → Form4=%d Form144=%d（近 %d 天）",
        sym, n_form4, n_144, INSIDER_WINDOW_DAYS,
    )
    return out


async def fetch_sec_events(ticker: str, *, months: int = DEFAULT_MONTHS) -> dict:
    """`sec_filings` 维度的一站式取数（美股专属维度，文档 §2.2 A 表）。

    Returns:
        {"symbol","cik","company","form_counts":{form:n},
         "negative":[...], "placements":[...], "reductions":[...],
         "blockholders":[...], "filings_total": int}
    """
    sym = str(ticker).strip().upper().split(".")[0]
    cik = await resolve_cik(ticker)
    if not cik:
        logger.warning("fetch_sec_events 无法解析 CIK: %s", sym)
        return {
            "symbol": sym, "cik": "", "company": "",
            "form_counts": {}, "negative": [], "placements": [],
            "reductions": [], "blockholders": [], "filings_total": 0,
        }

    all_filings = await fetch_submissions(ticker, months=months)
    form_counts: dict[str, int] = {}
    for f in all_filings:
        form_counts[f["form"]] = form_counts.get(f["form"], 0) + 1

    negative, placements, reductions = await asyncio.gather(
        fetch_negative_events(ticker, months=months),
        fetch_placements(ticker, months=months),
        fetch_reductions(ticker, months=months),
    )
    blockholders = [
        f for f in all_filings if f["form"].upper() in BLOCKHOLDER_FORMS
    ]

    return {
        "symbol": sym,
        "cik": cik,
        "company": all_filings[0]["company"] if all_filings else "",
        "form_counts": dict(sorted(form_counts.items(), key=lambda kv: -kv[1])),
        "negative": negative,
        "placements": placements,
        "reductions": reductions,
        "blockholders": blockholders,
        "filings_total": len(all_filings),
    }


__all__ = [
    "SecEdgarError",
    "resolve_cik",
    "fetch_submissions",
    "fetch_8k_items",
    "fetch_company_facts",
    "fetch_impairments",
    "fetch_negative_events",
    "fetch_placements",
    "fetch_reductions",
    "fetch_sec_events",
    "clear_cache",
    "ITEM_8K",
    "LATE_FILING_FORMS",
    "PLACEMENT_FORMS",
    "INSIDER_FORMS",
    "IMPAIRMENT_TAGS",
    "SEC_USER_AGENT",
    "DEFAULT_MONTHS",
]
