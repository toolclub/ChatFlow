"""公告三层判定（设计文档 §2.3）—— 数据型维度的「大脑」，防 token 打爆的核心。

问题规模：平安银行 1733 条公告、宁德时代 1780 条。全喂 LLM 必爆。

    第 1 层 · 规则（0 次 LLM，纯函数，<1s）
        复用 quant.data.announcement.classify_notice(ann_type, title)：
          - 退市风险警示 / 强制退市 → 规则直接定死 severity=-1.0
          - 业绩预告「首亏/续亏/预减」 → 东财硬结构化枚举，规则定级（confidence=structured）
          - SEC 8-K Item 4.02 → SEC 官方分类，规则定级（confidence=structured）
          - 董事会决议 / 独董述职 / 募集资金存放… → is_noise=True，**直接丢弃，不进 LLM**
            （实测占 60-70%，这一刀省下的就是 token）

    第 2 层 · 候选池（仍是规则）
        needs_llm=True 的模糊语义（「爆雷」「质押冻结」「减持进展」）+ 近
        PREDICTION_ANNOUNCE_MONTHS(24) 个月窗口 + 按日期倒序截断到 MAX_CANDIDATES(80)。

    第 3 层 · LLM 精判（只判候选池，分批并发）
        批大小 ≤ PREDICTION_ANN_BATCH(30)（实测 30 条 ≈ 14s；60 条 reasoning token 飙到
        2776 且有截断风险 —— 别往上调）。Semaphore(PREDICTION_ANN_CONCURRENCY)。
        confidence=llm_inferred。

    缓存（关键）
        announcement_cache 按 (market, symbol, ann_date, title_hash) 唯一。同一条公告只
        LLM 打分一次；缓存期内复跑同一只股票 ≈ 0 次 LLM 调用。

成本控制的两个设计决定（别改掉，改了就烧钱）：
  1. `announcements_full` 维度**不跑 LLM**（设计文档 §2.2：「单独勾选时只做时间线展示」）。
     它是另外三个风险维度的数据底座，自己只出规则层结果 + score=0.0，
     否则它的候选池会与 negative_event/private_placement/stake_reduction **完全重叠**，
     同一批标题被判 2 次。
  2. 因为 classify_notice 给每条公告只贴**一个** event_type，上述三个风险维度的候选池
     天然互斥 → 并发跑也不会重复判同一条。

诚实标注（用户拍板：口径缺口可以接受，但不许假装精确）：
  - confidence（domain.Confidence 三值枚举）：
        structured   规则层判定 —— 依据是数据源的**结构化字段**（东财「公告类型」列、
                     业绩预告「预告类型」枚举、SEC 8-K Item 编号），0 次 LLM
        llm_inferred LLM 层判定 —— 依据只有标题 + 类型（**不解析 PDF 正文**）
        estimated    LLM 判定失败时的兜底：negative_score=0（不计入事件分），reason 写明原因
  - source_note 必须随卡片透出：「基于公告标题与公告类型分类，未解析 PDF 正文」

铁律：#5 DB 全 async · #6 LLM 必须 astream（含 THINK 前缀剥离）· #9 异常不吞
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import date, datetime
from typing import Awaitable, Callable, Iterable, Sequence

from config import CHAT_MODEL
from db.prediction_store import get_announcement_cache, put_announcement_cache
from graph.prediction.config import (
    PREDICTION_ANN_BATCH,
    PREDICTION_ANN_CACHE_DAYS,
    PREDICTION_ANN_CONCURRENCY,
    PREDICTION_ANNOUNCE_MONTHS,
    PREDICTION_EVENT_DECAY_TAU_DAYS,
    PREDICTION_EVENT_WEIGHTS,
    PREDICTION_KAPPA_LOCKUP,
    PREDICTION_KAPPA_NEGATIVE,
    PREDICTION_KAPPA_PLACEMENT,
    PREDICTION_KAPPA_REDUCTION,
    PREDICTION_PLACEMENT_DILUTION_BASE,
    PREDICTION_PLACEMENT_DILUTION_FALLBACK,
    PREDICTION_PLACEMENT_DISCOUNT_BASE,
    PREDICTION_REDUCTION_RATIO_BASE,
)
from llm.chat import get_chat_llm
from quant.data.announcement import (
    EVENT_NEGATIVE,
    EVENT_BUYBACK,
    EVENT_LOCKUP,
    EVENT_NEUTRAL,
    EVENT_PLACEMENT,
    EVENT_REDUCTION,
    classify_notice,
    title_hash,
)

logger = logging.getLogger("graph.prediction.announcements")

# 推理模型（DeepSeek-R1 / GLM-Z1）thinking token 前缀 —— 与 news_search.py / llm/client.py 一致。
# 不剥离会导致 json.loads 失败（设计文档 §2.3 实测已复现）。
_THINK_PREFIX = "\x00THINK\x00"

_DeltaCallback = Callable[[str], Awaitable[None]]

# ── 契约常量 ──────────────────────────────────────────────────────────────────

# domain.Confidence 的三个合法值（Literal["structured","llm_inferred","estimated"]）。
# ★ 任务书里说的「rule 硬数据」= 这里的 structured（判定依据是数据源的结构化字段，0 次 LLM）。
CONF_STRUCTURED = "structured"
CONF_LLM = "llm_inferred"
CONF_ESTIMATED = "estimated"

# announcement_cache.judged_by 列（rule 行永不过期；llm 行按 PREDICTION_ANN_CACHE_DAYS 过期重判）
JUDGED_RULE = "rule"
JUDGED_LLM = "llm"

# 候选池硬上限（设计文档 §2.3 第 2 层：实测 *ST元道 3 年剩 50-80 条）。
# 超出则按公告日期倒序保留最近的 —— 三个月前的一纸问询函对 7 日预测的边际信息 ≈ 0。
MAX_CANDIDATES: int = 80

# 单维度最多**留存**多少条 item（进 FilingDimensionResult → JSONB → SSE）。
# 平安银行全历史 1733 条公告 × ~300B ≈ 500KB —— 一行 JSONB 和一条 SSE 都不该这么胖。
# 超出按日期倒序截断（老公告的时间衰减权重本就 ≈ 0，对 7 日预测没有信息量）；
# total_fetched 仍如实上报**原始条数**，不粉饰。
MAX_ITEMS: int = 200

# UI 必须显示的口径说明（设计文档 §2.4：不解析 PDF 正文）
SOURCE_NOTE_CN = "基于公告标题与公告类型分类，未解析 PDF 正文；大股东减持/违规为公司自主披露口径，非监管处罚数据库"
SOURCE_NOTE_US = "基于 SEC submissions 元数据（form 类型 / 8-K Item 编号 / XBRL 数值），未解析 filing 正文"

# 维度 key（与 dimensions.DIMENSION_REGISTRY 对齐；此处独立声明避免多一层 import）
DIM_ANNOUNCEMENTS_FULL = "announcements_full"
DIM_NEGATIVE = "negative_event"
DIM_PLACEMENT = "private_placement"
DIM_REDUCTION = "stake_reduction"
DIM_SEC = "sec_filings"
DIM_LOCKUP = "lockup_buyback"

# 维度 → 事件分的 κ（饱和系数，设计文档 §5.3）
_KAPPA_BY_EVENT: dict[str, float] = {
    EVENT_NEGATIVE: PREDICTION_KAPPA_NEGATIVE,
    EVENT_PLACEMENT: PREDICTION_KAPPA_PLACEMENT,
    EVENT_REDUCTION: PREDICTION_KAPPA_REDUCTION,
    EVENT_LOCKUP: PREDICTION_KAPPA_LOCKUP,
}

# 维度 → 它贡献的事件分通道（PREDICTION_EVENT_WEIGHTS 的 key）
DIMENSION_EVENT_CHANNEL: dict[str, str] = {
    DIM_NEGATIVE: "negative",
    DIM_PLACEMENT: "placement",
    DIM_REDUCTION: "reduction",
    DIM_LOCKUP: "lockup",
}

# 三个风险维度（sec_filings 只有在它们**都没勾**时才自己算分，否则会与它们双重计数 ——
# 美股下 negative_event/private_placement/stake_reduction 的 fetcher 内部也是走 sec_edgar）
RISK_DIMENSIONS: frozenset[str] = frozenset(
    (DIM_NEGATIVE, DIM_PLACEMENT, DIM_REDUCTION)
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. 纯函数：时间衰减 / 饱和函数（设计文档 §5.3）
# ══════════════════════════════════════════════════════════════════════════════

def _days_ago(ann_date: str) -> float | None:
    """公告距今天数；日期缺失/不可解析 → None（铁律 #9：不静默造数）。"""
    if not ann_date:
        return None
    try:
        d = datetime.strptime(str(ann_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.warning("公告日期不可解析: %r", ann_date)
        return None
    return max(0.0, (date.today() - d).days)


def decay_weight(ann_date: str, tau_days: float = PREDICTION_EVENT_DECAY_TAU_DAYS) -> float:
    """时间衰减权重 w = exp(-Δt / τ)，τ=45 天 → 半衰期 ≈ 31 天。

    日期未知 → 1.0（保守：不因为缺日期就把一条负面事件悄悄抹掉）。
    """
    delta = _days_ago(ann_date)
    if delta is None:
        return 1.0
    if tau_days <= 0:
        return 1.0
    return float(math.exp(-delta / tau_days))


def saturate(x_sum: float, kappa: float) -> float:
    """通用饱和函数 S = -(1 - exp(-Σxᵢwᵢ / κ))，S ∈ (-1, 0]。

    单调、有界、可解释：一堆 -0.2 的小事叠不成 -1，一条公告也刷不爆。
    """
    if x_sum <= 0 or kappa <= 0:
        return 0.0
    return float(-(1.0 - math.exp(-x_sum / kappa)))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ══════════════════════════════════════════════════════════════════════════════
# 2. 第 1 层：规则判定 + 归一化为 FilingItem
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_classified(raw: dict) -> dict:
    """给没经过 classify 的原始条目补上规则判定字段。

    quant.data 的 fetch_negative_events / fetch_placements / fetch_reductions 已经带了
    event_type/severity/needs_llm；fetch_stock_notices(classify=False) 和 SEC 的部分条目没有。
    """
    if "needs_llm" in raw and "event_type" in raw:
        return raw
    verdict = classify_notice(raw.get("ann_type", ""), raw.get("title", ""))
    return {**verdict, **raw}      # raw 优先：数据源给的硬字段不能被规则覆盖


def _reduction_ratio(raw: dict) -> float | None:
    """减持占总股本比例 → **小数**（0.015 = 1.5%）。

    东财 pct_of_total 是百分数（1.5 表示 1.5%），而 config 的
    PREDICTION_REDUCTION_RATIO_BASE=0.01 是小数口径 —— 不换算会把强度放大 100 倍。
    """
    pct = raw.get("pct_of_total")
    if pct is None:
        return None
    try:
        return float(pct) / 100.0
    except (TypeError, ValueError):
        logger.warning("减持比例不可解析: %r", pct)
        return None


def _rule_item(raw: dict) -> dict:
    """规则层可直接定案的条目 → FilingItem dict（0 次 LLM）。"""
    severity = raw.get("severity")
    score = 0.0 if severity is None else _clamp(float(severity), -1.0, 0.0)

    # 数据源自称 hard（业绩预告枚举 / SEC Item 编号 / 东财减持明细）与「规则命中公告类型」
    # 统一落到 structured：判定依据都是**结构化字段**，不是 LLM 猜的。
    return _filing_item(
        raw,
        event_type=str(raw.get("event_type") or EVENT_NEUTRAL),
        negative_score=score,
        reason=str(raw.get("reason") or ""),
        confidence=CONF_STRUCTURED,
    )


def _filing_item(
    raw: dict,
    *,
    event_type: str,
    negative_score: float,
    reason: str,
    confidence: str,
) -> dict:
    """统一组装 domain.FilingItem 的字段（多一个都不加：FilingItem 是 pydantic 模型，
    额外键在 FilingItem(**d) 时会被丢弃，塞了也传不到前端）。"""
    ann_date = str(raw.get("ann_date") or "")
    return {
        "title": str(raw.get("title") or ""),
        "date": ann_date,
        "ann_type": str(raw.get("ann_type") or ""),
        "url": str(raw.get("url") or ""),
        "event_type": event_type,
        "negative_score": _clamp(float(negative_score or 0.0), -1.0, 0.0),
        "reason": reason,
        "source": str(raw.get("source") or ""),
        "confidence": confidence,
        "dilution_ratio": raw.get("dilution_ratio"),
        "discount_rate": raw.get("discount_rate"),
        "reduction_ratio": _reduction_ratio(raw),
        "decay_weight": round(decay_weight(ann_date), 6),
        # 解禁/回购的强度输入。必须同时在 domain.FilingItem 里声明，否则会被 pydantic 丢掉。
        "lockup_pct": raw.get("lockup_pct"),
        "days_until": raw.get("days_until"),
        "support_ratio": raw.get("support_ratio"),
    }


def _in_window(ann_date: str, months: int) -> bool:
    """候选池时间窗口（第 2 层）。日期缺失 → 保留（宁可多判一条，也不漏掉爆雷）。"""
    if months <= 0:
        return True
    delta = _days_ago(ann_date)
    if delta is None:
        return True
    return delta <= months * 31


# ══════════════════════════════════════════════════════════════════════════════
# 3. 第 3 层：LLM 精判（astream + THINK 剥离 + 分批并发）
# ══════════════════════════════════════════════════════════════════════════════

_JUDGE_SYSTEM = (
    "你是 A 股/美股公告风险判定器。给你一批公告（序号、日期、公告类型、规则初判、标题），"
    "逐条判断它对该股未来 1-2 周股价的**负面影响强度**。\n"
    "评分标尺（negative_score，范围 -1..0，0 表示无负面/常规披露）：\n"
    "  0      常规经营/治理公告、利好、仅作波动说明\n"
    "  -0.2~-0.4  轻微：股份质押、小额诉讼、交易异常波动说明、减持预披露（未实施）\n"
    "  -0.5~-0.7  中等：监管问询函/关注函、账户或股份冻结、减持实施中、业绩下滑、增发预案\n"
    "  -0.8~-1.0  严重：立案调查、行政处罚、财务造假、重大违约、退市风险警示、强制退市\n"
    "event_type 必须是以下之一，默认沿用「规则初判」，只有明显判错时才改："
    "negative（负面/爆雷）、private_placement（增发/配股）、stake_reduction（股东减持）、neutral（无影响）。\n"
    '只输出 JSON 数组，每项 {"i": 序号, "event_type": "...", "negative_score": 数字, "reason": "一句简短理由"}。'
    "不要 Markdown 代码块，不要解释，不要多余文字。"
)

_VALID_EVENT_TYPES = {EVENT_NEGATIVE, EVENT_PLACEMENT, EVENT_REDUCTION, EVENT_NEUTRAL}


def _parse_judgements(raw: str) -> dict[int, dict]:
    """容错解析 LLM 的 JSON 数组（沿用 news_search._parse_query_list 的 find-[...] 思路）。"""
    if not raw:
        return {}

    def _from(obj) -> dict[int, dict]:
        out: dict[int, dict] = {}
        if not isinstance(obj, list):
            return out
        for el in obj:
            if not isinstance(el, dict):
                continue
            try:
                idx = int(el.get("i"))
            except (TypeError, ValueError):
                continue
            etype = str(el.get("event_type") or "").strip()
            if etype not in _VALID_EVENT_TYPES:
                etype = ""          # 非法枚举 → 回退到规则初判（调用方兜底）
            try:
                score = float(el.get("negative_score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            out[idx] = {
                "event_type": etype,
                "negative_score": _clamp(score, -1.0, 0.0),
                "reason": str(el.get("reason") or "").strip(),
            }
        return out

    try:
        return _from(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = raw.find("["), raw.rfind("]")
    if 0 <= start < end:
        try:
            return _from(json.loads(raw[start : end + 1]))
        except (json.JSONDecodeError, TypeError):
            pass
    logger.warning("公告 LLM 判定 JSON 解析失败（raw_len=%d）", len(raw))
    return {}


async def _judge_batch(
    llm,
    symbol: str,
    market: str,
    batch: Sequence[tuple[int, dict]],
    on_delta: _DeltaCallback | None,
    diag: dict,
) -> dict[int, dict]:
    """一批（≤ PREDICTION_ANN_BATCH 条）公告的 LLM 精判。永不抛（异常写进 diag）。"""
    lines = []
    for idx, raw in batch:
        lines.append(
            f"{idx}. [{raw.get('ann_date') or '日期未知'}] "
            f"类型={raw.get('ann_type') or '未知'} | 规则初判={raw.get('event_type') or 'neutral'} "
            f"| 标题：{raw.get('title') or ''}"
        )
    user_prompt = (
        f"标的：{symbol}（{market}）\n"
        f"共 {len(batch)} 条公告，请逐条判定：\n" + "\n".join(lines) + "\n\n请输出 JSON 数组。"
    )
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    parts: list[str] = []
    try:
        # 铁律 #6：LLM 必须 astream，绝不 ainvoke
        async for delta in llm.astream(messages, temperature=0.1, timeout=120.0):
            if isinstance(delta, dict):     # usage 统计块（astream 最后一个 chunk）
                continue
            if not isinstance(delta, str):
                continue
            if delta.startswith(_THINK_PREFIX):
                # 推理模型的 thinking token：剥离前缀后转发展示，**不参与 JSON 解析**
                text = delta[len(_THINK_PREFIX):]
                if text and on_delta is not None:
                    await on_delta(text)
            else:
                parts.append(delta)
    except asyncio.CancelledError:
        raise                                # 用户 stop / 暂停：原样传播，不收敛成"判定失败"
    except Exception as exc:
        # 铁律 #9：异常不吞；用已累积的部分尽力解析，同时把故障暴露给调用方
        logger.warning("公告 LLM 判定流式失败 | symbol=%s | batch=%d | %s", symbol, len(batch), exc)
        diag["error"] = str(exc)

    raw_text = "".join(parts)
    diag["raw_len"] = len(raw_text)
    return _parse_judgements(raw_text)


# ══════════════════════════════════════════════════════════════════════════════
# 4. 编排：三层判定主入口
# ══════════════════════════════════════════════════════════════════════════════

async def judge_announcements(
    symbol: str,
    market: str,
    raw_items: Iterable[dict],
    *,
    dimension: str,
    months: int = PREDICTION_ANNOUNCE_MONTHS,
    llm=None,
    on_thinking: _DeltaCallback | None = None,
    use_cache: bool = True,
) -> tuple[list[dict], dict]:
    """三层判定：规则 → 候选池 → LLM 精判（带 announcement_cache）。

    Args:
        symbol:     标的代码（缓存键的一部分；用什么口径传，缓存就按什么口径存）
        market:     cn_a / us_stock
        raw_items:  quant.data.* fetcher 的原始返回（dict 列表）
        dimension:  维度 key —— announcements_full **不跑 LLM**（时间线展示维度，
                    它的候选池与另外三个风险维度完全重叠，跑了就是白烧一遍 token）
        months:     候选池时间窗口（默认 PREDICTION_ANNOUNCE_MONTHS=24）
        llm:        注入的 LLM（None 且确有候选时才 get_chat_llm(CHAT_MODEL)）
        on_thinking: LLM reasoning token 回调（节点用 BaseNode.emit_thinking 包装）
        use_cache:  False 时跳过 announcement_cache（测试用）

    Returns:
        (items, stats)
        items —— domain.FilingItem 字段的 dict 列表，按日期倒序
        stats —— {"total_fetched","noise_dropped","rule_judged","candidates",
                  "cache_hits","llm_judged","llm_failed","llm_batches"}

    永不抛：LLM/缓存故障只降级（confidence=estimated + reason 写明原因），
    数据源故障由调用方（news_search._run_data_dimension）捕获 DATA_SOURCE_ERRORS 处理。
    """
    raws = [r for r in (raw_items or []) if isinstance(r, dict)]
    stats = {
        "total_fetched": len(raws),
        "noise_dropped": 0,
        "rule_judged": 0,
        "candidates": 0,
        "cache_hits": 0,
        "llm_judged": 0,
        "llm_failed": 0,
        "llm_batches": 0,
    }

    decided: list[dict] = []                 # 规则层 / 缓存命中，已定案
    pool: list[dict] = []                    # 待 LLM 精判的候选

    # ── 第 1 层：规则 ───────────────────────────────────────────────────────
    for raw in raws:
        item = _ensure_classified(raw)
        if item.get("is_noise"):
            stats["noise_dropped"] += 1
            continue
        if item.get("needs_llm") and dimension != DIM_ANNOUNCEMENTS_FULL:
            # ── 第 2 层：候选池（时间窗口过滤）─────────────────────────────
            if _in_window(item.get("ann_date", ""), months):
                pool.append(item)
                continue
            # 窗口外的模糊条目：不烧 LLM，规则给 0（reason 写清楚为什么没判）
            decided.append(_filing_item(
                item,
                event_type=str(item.get("event_type") or EVENT_NEUTRAL),
                negative_score=0.0,
                reason=f"超出 {months} 个月判定窗口，未做 LLM 精判（仅作历史展示）",
                confidence=CONF_STRUCTURED,
            ))
            stats["rule_judged"] += 1
            continue
        decided.append(_rule_item(item))
        stats["rule_judged"] += 1

    # 候选池上限：按日期倒序取最近的 MAX_CANDIDATES 条
    pool.sort(key=lambda x: x.get("ann_date") or "", reverse=True)
    if len(pool) > MAX_CANDIDATES:
        logger.info(
            "候选池 %d 条 > 上限 %d，截断保留最近的 | symbol=%s dim=%s",
            len(pool), MAX_CANDIDATES, symbol, dimension,
        )
        for dropped in pool[MAX_CANDIDATES:]:
            decided.append(_filing_item(
                dropped,
                event_type=str(dropped.get("event_type") or EVENT_NEUTRAL),
                negative_score=0.0,
                reason=f"候选池已达上限 {MAX_CANDIDATES} 条，本条未做 LLM 精判（仅作历史展示）",
                confidence=CONF_STRUCTURED,
            ))
        pool = pool[:MAX_CANDIDATES]
    stats["candidates"] = len(pool)

    # ── 缓存：命中即 0 次 LLM ────────────────────────────────────────────────
    pending: list[dict] = pool
    if pool and use_cache:
        keys = [
            (p.get("ann_date") or "", p.get("title_hash") or title_hash(p.get("title", "")))
            for p in pool
        ]
        cached = await get_announcement_cache(
            market, symbol, keys=keys, llm_ttl_days=float(PREDICTION_ANN_CACHE_DAYS),
        )
        pending = []
        for p in pool:
            key = (
                p.get("ann_date") or "",
                p.get("title_hash") or title_hash(p.get("title", "")),
            )
            row = cached.get(key)
            if row is None:
                pending.append(p)
                continue
            stats["cache_hits"] += 1
            decided.append(_filing_item(
                p,
                event_type=str(row.get("event_type") or p.get("event_type") or EVENT_NEUTRAL),
                negative_score=float(row.get("negative_score") or 0.0),
                reason=str(row.get("reason") or ""),
                confidence=(
                    CONF_LLM if row.get("judged_by") == JUDGED_LLM else CONF_STRUCTURED
                ),
            ))

    # ── 第 3 层：LLM 精判（分批 + Semaphore 并发）────────────────────────────
    if pending:
        indexed = list(enumerate(pending))
        batches = [
            indexed[i : i + PREDICTION_ANN_BATCH]
            for i in range(0, len(indexed), PREDICTION_ANN_BATCH)
        ]
        stats["llm_batches"] = len(batches)
        active_llm = llm or get_chat_llm(CHAT_MODEL, temperature=0.1)
        sem = asyncio.Semaphore(max(1, PREDICTION_ANN_CONCURRENCY))

        async def _run(batch):
            diag: dict = {}
            async with sem:
                got = await _judge_batch(active_llm, symbol, market, batch, on_thinking, diag)
            return batch, got, diag

        logger.info(
            "公告 LLM 精判启动 | symbol=%s dim=%s | 候选=%d 批=%d（batch=%d, 并发=%d）",
            symbol, dimension, len(pending), len(batches),
            PREDICTION_ANN_BATCH, PREDICTION_ANN_CONCURRENCY,
        )
        t0 = time.time()
        results = await asyncio.gather(
            *(_run(b) for b in batches), return_exceptions=True,
        )

        to_cache: list[dict] = []
        for res in results:
            if isinstance(res, BaseException):
                # _judge_batch 内部已收敛异常，走到这里说明是它自己炸了（不该发生）
                if isinstance(res, asyncio.CancelledError):
                    raise res
                logger.warning("公告 LLM 判定批次逃逸异常 | symbol=%s: %s", symbol, res)
                continue
            batch, got, diag = res
            err = diag.get("error") or ("模型未返回可解析 JSON" if not got else "")
            for idx, raw in batch:
                verdict = got.get(idx)
                if verdict is None:
                    # 判定失败 → **不当中性**：negative_score=0 但 confidence=estimated
                    # 且 reason 写明原因（把故障伪装成"这条没事"是最危险的谎）
                    stats["llm_failed"] += 1
                    decided.append(_filing_item(
                        raw,
                        event_type=str(raw.get("event_type") or EVENT_NEUTRAL),
                        negative_score=0.0,
                        reason=f"LLM 判定失败（{err or '未返回该条'}），未计入事件分",
                        confidence=CONF_ESTIMATED,
                    ))
                    continue
                stats["llm_judged"] += 1
                etype = verdict["event_type"] or str(raw.get("event_type") or EVENT_NEUTRAL)
                item = _filing_item(
                    raw,
                    event_type=etype,
                    negative_score=verdict["negative_score"],
                    reason=verdict["reason"],
                    confidence=CONF_LLM,
                )
                decided.append(item)
                to_cache.append({
                    "market": market,
                    "symbol": symbol,
                    "ann_date": item["date"],
                    "title_hash": raw.get("title_hash") or title_hash(item["title"]),
                    "title": item["title"],
                    "ann_type": item["ann_type"],
                    "url": item["url"],
                    "source": item["source"],
                    "event_type": item["event_type"],
                    "negative_score": item["negative_score"],
                    "reason": item["reason"],
                    "judged_by": JUDGED_LLM,
                })

        if to_cache and use_cache:
            # 只缓存 LLM 判定结果：规则层本来就 0 成本，缓存它纯属浪费写入
            written = await put_announcement_cache(to_cache)
            logger.info("公告判定缓存写入 %d/%d 条 | symbol=%s", written, len(to_cache), symbol)

        logger.info(
            "公告 LLM 精判完成 | symbol=%s dim=%s | 判定=%d 失败=%d | %.1fs",
            symbol, dimension, stats["llm_judged"], stats["llm_failed"], time.time() - t0,
        )

    decided.sort(key=lambda x: x.get("date") or "", reverse=True)
    if len(decided) > MAX_ITEMS:
        logger.info(
            "维度 item 数 %d > 留存上限 %d，按日期倒序截断（total_fetched 仍报原值）"
            " | symbol=%s dim=%s",
            len(decided), MAX_ITEMS, symbol, dimension,
        )
        decided = decided[:MAX_ITEMS]

    logger.info(
        "judge_announcements | symbol=%s dim=%s | 拉取=%d 噪音=%d 规则=%d 候选=%d "
        "缓存命中=%d LLM=%d 失败=%d",
        symbol, dimension, stats["total_fetched"], stats["noise_dropped"],
        stats["rule_judged"], stats["candidates"], stats["cache_hits"],
        stats["llm_judged"], stats["llm_failed"],
    )
    return decided, stats


# ══════════════════════════════════════════════════════════════════════════════
# 5. 事件分（设计文档 §5.3）—— engine 直接复用，不要各算一遍
# ══════════════════════════════════════════════════════════════════════════════

def _intensity(item: dict, channel: str) -> float:
    """单条事件的强度 x（已含时间衰减 w = exp(-Δt/τ)）。"""
    w = float(item.get("decay_weight") or decay_weight(item.get("date", "")))

    if channel == "negative":
        # xᵢ = |negative_score|
        return abs(float(item.get("negative_score") or 0.0)) * w

    if channel == "placement":
        # xⱼ = 摊薄比例/0.10 + max(0, 折价率)/0.20
        dilution = item.get("dilution_ratio")
        if dilution is None:
            # 拿不到总股本 → 保守兜底 0.05（UI 标「估算」，不假装精确）
            dilution = PREDICTION_PLACEMENT_DILUTION_FALLBACK
        discount = item.get("discount_rate")
        x = float(dilution) / PREDICTION_PLACEMENT_DILUTION_BASE
        if discount is not None:
            x += max(0.0, float(discount)) / PREDICTION_PLACEMENT_DISCOUNT_BASE
        return x * w

    if channel == "reduction":
        # x_k = 减持占总股本比例 / 1%（拿不到比例时按公告条数计 x_k = 1）
        ratio = item.get("reduction_ratio")
        x = 1.0 if ratio is None else float(ratio) / PREDICTION_REDUCTION_RATIO_BASE
        return x * w

    if channel == "lockup":
        # ★ 解禁是**未来**事件，权重必须按「临近度」而不是「过去多久」。
        #   直接套 decay_weight(未来日期) 会得到 exp(+Δt/τ) > 1 —— 越远的解禁反而权重越大，
        #   完全反了。这里改用 w = exp(-days_until/τ)：越临近，抛压越重。
        if item.get("event_type") == EVENT_BUYBACK:
            return 0.0          # 回购不走逐条通道，由 compute_dimension_score 做净额抵消
        pct = item.get("lockup_pct")
        if pct is None:
            return 0.0
        days = item.get("days_until")
        try:
            d = max(0.0, float(days)) if days is not None else 0.0
        except (TypeError, ValueError):
            d = 0.0
        w_ahead = math.exp(-d / PREDICTION_EVENT_DECAY_TAU_DAYS)
        return (float(pct) / 1.0) * w_ahead      # 占总市值 1% → x = 1

    return 0.0


def _channel_score(items: Sequence[dict], channel: str, event_type: str) -> float:
    """单通道饱和分 S ∈ (-1, 0]。"""
    kappa = _KAPPA_BY_EVENT.get(event_type, 2.0)
    x_sum = sum(
        _intensity(it, channel)
        for it in items
        if it.get("event_type") == event_type
    )
    return round(saturate(x_sum, kappa), 6)


def compute_dimension_score(
    dimension: str,
    items: Sequence[dict],
    *,
    siblings: Iterable[str] = (),
) -> tuple[float, str]:
    """维度饱和分 S ∈ (-1, 0] + 一句 summary（FilingDimensionResult.score/summary）。

    - announcements_full → **0.0**：它是数据底座/时间线展示维度，分数由另外三个风险维度
      承担；给它算分会与它们双重计数（同一批公告）。
    - sec_filings → 只有当 negative_event/private_placement/stake_reduction **一个都没勾**
      时才自己算综合分；否则同样是 0.0 避免双重计数（美股下那三个维度的 fetcher 内部
      也是走 sec_edgar，数据完全同源）。
    """
    n = len(items)
    negatives = [it for it in items if float(it.get("negative_score") or 0.0) < 0]
    estimated = sum(1 for it in items if it.get("confidence") == CONF_ESTIMATED)

    if dimension == DIM_ANNOUNCEMENTS_FULL:
        latest = items[0]["date"] if items else "—"
        summary = f"共 {n} 条公告（最近一条 {latest}）；作为风险维度的数据底座，不单独计分"
        return 0.0, summary

    # 解禁/回购：净额抵消，与其它维度的「逐条累加」语义不同，单独算。
    #
    #   x_sum = Σ(解禁占总市值% × exp(-days_until/τ))  −  Σ(回购占总股本%)
    #   S     = saturate(max(0, x_sum), κ_lockup)      ∈ (-1, 0]
    #
    # 「回购（托底）」= 把抛压减掉，而不是把预测推高 —— 事件分被钳在 [-1,0]（engine.py:540），
    # 本来就表达不了利多。这是一个**建模简化**，summary 里如实写出来，UI 会原样透出。
    if dimension == DIM_LOCKUP:
        lock_x = sum(_intensity(it, "lockup") for it in items)
        buy_x = 0.0
        for it in items:
            if it.get("event_type") != EVENT_BUYBACK:
                continue
            sup = it.get("support_ratio")
            if sup is None:
                continue
            try:
                buy_x += float(sup)
            except (TypeError, ValueError):
                logger.warning("回购比例不可解析: %r", sup)
        net = lock_x - buy_x
        score = round(saturate(max(0.0, net), _KAPPA_BY_EVENT[EVENT_LOCKUP]), 6)

        n_lock = sum(1 for it in items if it.get("event_type") == EVENT_LOCKUP)
        n_buy = sum(1 for it in items if it.get("event_type") == EVENT_BUYBACK)
        if not n_lock and not n_buy:
            return 0.0, "未来 90 天无解禁，也无在途回购计划"
        parts = [
            f"未来 90 天解禁 {n_lock} 笔（临近度加权强度 {lock_x:.2f}）"
            f"、回购 {n_buy} 笔（托底 {buy_x:.2f}）",
            f"净抛压 {max(0.0, net):.2f} → 饱和分 {score:.3f}",
        ]
        if buy_x > 0:
            parts.append("回购仅抵消抛压，不独立推高预测")
        return score, "；".join(parts)

    channel = DIMENSION_EVENT_CHANNEL.get(dimension)
    if channel:
        event_type = {
            "negative": EVENT_NEGATIVE,
            "placement": EVENT_PLACEMENT,
            "reduction": EVENT_REDUCTION,
        }[channel]
        score = _channel_score(items, channel, event_type)
        hits = sum(1 for it in items if it.get("event_type") == event_type)
        parts = [f"命中 {hits} 条（共 {n} 条候选），饱和分 {score:.3f}"]
        if negatives:
            worst = min(negatives, key=lambda x: float(x.get("negative_score") or 0.0))
            parts.append(f"最重：{worst.get('title', '')[:40]}（{worst.get('negative_score')}）")
        if estimated:
            parts.append(f"{estimated} 条 LLM 判定失败未计分")
        return score, "；".join(parts)

    if dimension == DIM_SEC:
        sib = set(siblings)
        if sib & RISK_DIMENSIONS:
            return 0.0, (
                f"共 {n} 条 SEC 备案；负面/增发/减持已由对应维度计分，本维度只做展示，不重复计分"
            )
        # 三个风险维度都没勾 → sec_filings 自己出综合分 F（同 §5.3 ④ 的加权）
        weights = PREDICTION_EVENT_WEIGHTS
        s_neg = _channel_score(items, "negative", EVENT_NEGATIVE)
        s_pp = _channel_score(items, "placement", EVENT_PLACEMENT)
        s_sr = _channel_score(items, "reduction", EVENT_REDUCTION)
        score = _clamp(
            weights.get("negative", 0.5) * s_neg
            + weights.get("placement", 0.2) * s_pp
            + weights.get("reduction", 0.3) * s_sr,
            -1.0, 0.0,
        )
        return round(score, 6), (
            f"共 {n} 条 SEC 备案；负面 {s_neg:.3f} / 增发 {s_pp:.3f} / 减持 {s_sr:.3f} "
            f"→ 综合 {score:.3f}"
        )

    # 未知数据型维度（如尚未实现 fetcher 的 lockup_buyback）：不计分
    return 0.0, f"共 {n} 条记录（该维度暂不参与事件分计算）"


def aggregate_event_score(
    filing_digest: Sequence[dict],
) -> tuple[float, dict[str, float], dict[str, float]]:
    """事件综合分 F = clamp(Σ wᵢ·Sᵢ, -1, 0)（设计文档 §5.3 ④）。

    engine 直接调这个，别自己再实现一遍 —— 权重/κ/衰减的单一真相源在 config + 本模块。

    Returns:
        (F, event_scores, event_weights)
        event_scores  {"negative": -0.39, "placement": -0.12, "reduction": -0.25}
        event_weights {"negative": 0.5,   "placement": 0.2,   "reduction": 0.3}
        —— 两者原样进 ModelParams.event_scores / event_weights，供前端公式卡片展示。
    """
    weights = dict(PREDICTION_EVENT_WEIGHTS)
    scores: dict[str, float] = {"negative": 0.0, "placement": 0.0, "reduction": 0.0}

    for dim in filing_digest or []:
        if dim.get("failed"):
            continue
        channel = DIMENSION_EVENT_CHANNEL.get(str(dim.get("dimension") or ""))
        score = float(dim.get("score") or 0.0)
        if channel:
            # 同通道多维度（理论上不会有）取更重的那个，不做加法（加法会突破饱和上界）
            scores[channel] = min(scores[channel], score)
        elif dim.get("dimension") == DIM_SEC and score < 0:
            # sec_filings 只有在三个风险维度都没勾时才有非 0 分（见 compute_dimension_score），
            # 此时它已经是加权后的 F —— 直接返回，不再乘一遍权重
            return round(_clamp(score, -1.0, 0.0), 6), scores, weights

    f = sum(weights.get(k, 0.0) * v for k, v in scores.items())
    return round(_clamp(f, -1.0, 0.0), 6), scores, weights


def to_event_markers(
    filing_digest: Sequence[dict], *, limit: int = 30,
) -> list[dict]:
    """K 线事件打点（domain.EventMarker）：只标**有负面分**的条目，按严重度取 top N。"""
    markers: list[dict] = []
    for dim in filing_digest or []:
        if dim.get("failed"):
            continue
        dim_key = str(dim.get("dimension") or "")
        for it in dim.get("items") or []:
            score = float(it.get("negative_score") or 0.0)
            if score >= 0 or not it.get("date"):
                continue
            markers.append({
                "date": it["date"],
                "event_type": str(it.get("event_type") or EVENT_NEGATIVE),
                "title": str(it.get("title") or "")[:80],
                "score": score,
                "dimension": dim_key,
            })
    markers.sort(key=lambda m: (m["score"], m["date"]))     # 最重 + 最早在前
    return markers[: max(1, limit)]


def source_note(market: str) -> str:
    """UI 必须显示的口径说明（诚实标注，用户拍板）。"""
    return SOURCE_NOTE_US if market == "us_stock" else SOURCE_NOTE_CN


__all__ = [
    "judge_announcements",
    "compute_dimension_score",
    "aggregate_event_score",
    "to_event_markers",
    "decay_weight",
    "saturate",
    "source_note",
    "MAX_CANDIDATES",
    "CONF_STRUCTURED",
    "CONF_LLM",
    "CONF_ESTIMATED",
    "JUDGED_RULE",
    "JUDGED_LLM",
    "DIMENSION_EVENT_CHANNEL",
    "RISK_DIMENSIONS",
    "SOURCE_NOTE_CN",
    "SOURCE_NOTE_US",
]
