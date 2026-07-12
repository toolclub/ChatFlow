"""资讯搜索 helper（Tavily）—— 预测图专用。

与聊天轮次的 web_search 工具同源（Tavily basic + chunks + raw_content + answer），
但有三个关键差异（见 graph/prediction/config.py 顶部注释）：

  1. 全量不截取：每条结果保留完整 raw_content（full_content 字段），
     不像 tools/builtin/web_search.py._normalize_result 把原文切成 ≤4500 字符的 chunks。
     用户明确要求「拿全部数据」——情绪打分需要完整上下文，截断会丢信息。
  2. per-prediction 预算：Redis key `chatflow:prediction:websearch:{prediction_id}`，
     独立于聊天轮次的 `chatflow:websearch:count:` scope（否则 12 维 × 多 query 会打爆
     聊天 5 次/轮上限）。超额返回降级 {answer:"", results:[], note:"预算耗尽"}。
  3. 12 维资讯：LLM 生成 query → Tavily 全量搜索 → LLM 情绪打分（-1..1），
     聚合 news_sentiment 供蒙特卡洛漂移修正。

v2 新增：**按维度 kind 分流**（设计文档 §2.1/§3.3/§4）
  - kind="search" → 走下面这条已实测跑通的 Tavily 路径（**未改动**）
  - kind="data"   → _run_data_dimension：按 Dimension.fetcher 反射直调 quant.data.*
                    → graph/prediction/announcements.py 三层判定 → FilingDimensionResult
  两类维度在**同一个 asyncio.gather** 里并发，共用 Semaphore / counter / 进度回调。
  ★ 数据型维度**不消耗 Tavily 预算**（budget_event 与它无关）：调用量固定（每维 1-2 次 HTTP），
    失败模式也不同（akshare 限流 ≠ Tavily 429）。
  ★ 每次调数据源**前后**各发一条 TOOL_TRACE（用户要的「看到模型调用工具」，设计文档 §4.2）；
    每张数据卡片发 DATA_ITEM（与 NEWS_ITEM 平级）。

并发（本轮修复「卡住感」）：
  12 维不再串行（实测 6~16 分钟、SSE 上最长 198 秒零字节），改为
  asyncio.gather + Semaphore(PREDICTION_NEWS_CONCURRENCY) 并发跑。
  - 每维**开跑前**就发一条进度（on_progress），不是搜完才发 —— 空洞被
    "正在搜索：地缘政治 (3/12)" 填满。
  - digest 顺序仍严格跟随 dims 入参顺序（gather 保序），与串行版一致。
  - 单维 thinking 按维缓冲、维度结束时整块推送：并发下逐 token 直推会把 12 路
    reasoning 交错成乱码，且会往 event_log 灌几千行。
  - 单维失败（Tavily/LLM 不可用）不再被默默当成"中性情绪"：标 failed + error，
    并从 news_sentiment 均值里剔除；全维失败 → raise NewsSearchUnavailable。

合规性（spec.md 铁律）：
  - #2 结构化数据走独立字段（NewsItem.full_content），不塞 message.content
  - #4 所有 DB/Redis 操作在 async def 内
  - #5 所有 LLM 调用走 llm.astream（generate_queries / score_sentiment）
  - #9 异常不吞，except 至少 logger.warning
  - 模型无关：get_chat_llm(CHAT_MODEL)，不硬编码 deepseek
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable, Sequence

import httpx

from config import CHAT_MODEL
from graph.prediction.announcements import (
    compute_dimension_score,
    judge_announcements,
    source_note,
)
from graph.prediction.config import (
    DIMENSION_LABELS,
    NEWS_DIMENSIONS,
    PREDICTION_ANNOUNCE_MONTHS,
    PREDICTION_NEWS_CONCURRENCY,
    PREDICTION_NEWS_THINKING_MAX_CHARS,
    PREDICTION_PHASE_NEWS,
    PREDICTION_WEBSEARCH_BUDGET,
    TAVILY_API_KEY,
    TAVILY_CHUNKS_PER_SOURCE,
    TAVILY_DAYS,
    TAVILY_MAX_RESULTS,
    TAVILY_RETRY_DELAY,
    TAVILY_RETRY_TIMES,
    TAVILY_TIMEOUT,
    TAVILY_URL,
)
from graph.prediction.dimensions import get_dimension
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.prediction.news_search")

# 推理模型（GLM-Z1 / DeepSeek-R1）thinking token 前缀，与 llm/client.py 一致
_THINK_PREFIX = "\x00THINK\x00"

# 类型别名：流式 token 回调（节点用 BaseNode.emit_thinking 包装）
_DeltaCallback = Callable[[str], Awaitable[None]]
# 类型别名：进度回调（节点用 SSEEventType.PREDICTION_PROGRESS 事件 + 落库包装）
_ProgressCallback = Callable[[dict], Awaitable[None]]
# 类型别名：单条 payload 回调（DATA_ITEM / TOOL_TRACE —— 节点包上 SSE 事件类型 + 落库）
_ItemCallback = Callable[[dict], Awaitable[None]]

# Redis 不可用时的进程内兜底计数（key=prediction_id）
_FALLBACK_COUNTS: dict[str, int] = {}

# 预算耗尽的降级 note（tavily_search 返回值里的标记，跨函数复用，避免字面量散落）
_BUDGET_NOTE = "预算耗尽"


class NewsSearchUnavailable(RuntimeError):
    """全部资讯维度均失败（Tavily / 网络不可用）。

    节点据此把 state.error 置上，编排层落 failed —— 而不是拿一堆"中性 0 分"
    的空维度继续跑预测（那会让用户以为资讯已纳入，实际全是噪声）。
    """

    def __init__(self, message: str, failures: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.failures = failures or []


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tavily 搜索（全量数据 + per-prediction 预算）
# ══════════════════════════════════════════════════════════════════════════════

async def tavily_search(
    query: str,
    prediction_id: str,
    max_results: int = TAVILY_MAX_RESULTS,
) -> dict:
    """单次 Tavily 搜索，返回完整数据（不截断）。

    返回结构：
        正常: {"answer": str, "results": [{title, url, snippet, full_content}, ...]}
        降级: {"answer": "", "results": [], "note": "..."}  # 预算耗尽 / 配置缺失 / 错误

    预算：递增 Redis 计数 `chatflow:prediction:websearch:{prediction_id}`，
    超过 PREDICTION_WEBSEARCH_BUDGET 即返回降级结果，不再调用 Tavily。
    """
    query_norm = (query or "").strip()

    # ── per-prediction 预算检查（先递增再比较） ──────────────────────────────
    count = await _incr_prediction_count(prediction_id)
    if count > PREDICTION_WEBSEARCH_BUDGET:
        logger.warning(
            "news_search 预算耗尽 | pred=%s | count=%d/%d | query='%s'",
            prediction_id, count, PREDICTION_WEBSEARCH_BUDGET, query_norm,
        )
        return {"answer": "", "results": [], "note": _BUDGET_NOTE}

    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY 未配置 | pred=%s | query='%s'", prediction_id, query_norm)
        return {"answer": "", "results": [], "note": "TAVILY_API_KEY 未配置"}

    last_exc: Exception | None = None

    for attempt in range(1, TAVILY_RETRY_TIMES + 1):
        try:
            async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT) as client:
                resp = await client.post(
                    TAVILY_URL,
                    json={
                        "api_key": TAVILY_API_KEY,
                        "query": query_norm,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "chunks_per_source": TAVILY_CHUNKS_PER_SOURCE,
                        "include_raw_content": True,   # 拿完整原文，不截断
                        "include_answer": True,
                        "days": TAVILY_DAYS,          # 资讯实时性：只返回最近 N 天发布的文章
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            answer = data.get("answer") or ""
            results_raw = data.get("results", []) or []
            # 关键差异：full_content 保留完整 raw_content，不做 4500 字符切片
            results = [_normalize_result(r) for r in results_raw]

            logger.info(
                "news_search tavily 完成 | pred=%s | query='%s' | answer_len=%d | results=%d",
                prediction_id, query_norm, len(answer), len(results),
            )
            return {"answer": answer, "results": results}

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning(
                "news_search 连接失败（第%d/%d次）| query='%s' | %s",
                attempt, TAVILY_RETRY_TIMES, query_norm, exc,
            )
            if attempt < TAVILY_RETRY_TIMES:
                wait = TAVILY_RETRY_DELAY * (2 ** (attempt - 1))
                logger.info("news_search 等待 %.1f 秒后重试...", wait)
                await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            logger.error(
                "news_search HTTP错误 | query='%s' | status=%d",
                query_norm, exc.response.status_code,
            )
            return {"answer": "", "results": [], "note": f"Tavily HTTP {exc.response.status_code}"}

        except Exception as exc:
            # 铁律 #9：异常不吞
            logger.error(
                "news_search 未知异常 | query='%s' | %s", query_norm, exc, exc_info=True,
            )
            return {"answer": "", "results": [], "note": f"搜索失败: {exc}"}

    logger.error(
        "news_search 重试耗尽 | query='%s' | last_error=%s", query_norm, last_exc,
    )
    return {"answer": "", "results": [], "note": "连接失败"}


def _normalize_result(r: dict) -> dict:
    """统一 Tavily 单条结果格式。

    与 tools/builtin/web_search.py._normalize_result 的区别：
      - full_content 保留 **完整** raw_content（用户要求拿全部数据），
        不切成 ≤4500 字符的 chunks 列表。
      - 不再产出 chunks 字段（情绪打分用完整原文，不需要分段兜底）。
    """
    return {
        "title":         r.get("title", ""),
        "url":           r.get("url", ""),
        "snippet":       r.get("content", ""),          # Tavily content（摘要）
        "full_content":  r.get("raw_content", "") or "",  # 完整原文，不截断
    }


async def _incr_prediction_count(prediction_id: str) -> int:
    """递增 per-prediction 搜索计数，返回新值。

    优先用 Redis（跨 worker 共享），Redis 不可用时降级为进程内 dict。
    不复用 db.redis_state.incr_websearch_count —— 那个用的 key 前缀是
    `chatflow:websearch:count:` 且带 30min TTL，属于聊天轮次预算域，
    与预测的 `chatflow:prediction:websearch:` 域隔离。
    """
    key = f"chatflow:prediction:websearch:{prediction_id}"
    try:
        from db.redis_state import _get_redis
        r = _get_redis()
        new_val = await r.incr(key)
        if new_val == 1:
            await r.expire(key, 7200)  # 2h，覆盖一次预测运行 + 重试余量
        return int(new_val)
    except Exception as exc:
        # 铁律 #9：异常不吞；降级为进程内计数
        logger.warning(
            "news_search 预算计数 Redis 不可用，降级进程内 | pred=%s | %s",
            prediction_id, exc,
        )
        _FALLBACK_COUNTS[prediction_id] = _FALLBACK_COUNTS.get(prediction_id, 0) + 1
        return _FALLBACK_COUNTS[prediction_id]


# ══════════════════════════════════════════════════════════════════════════════
# 2. LLM 查询生成（astream，token 经 on_delta 回调转发给节点 emit_thinking）
# ══════════════════════════════════════════════════════════════════════════════

_QUERY_GEN_SYSTEM = (
    "你是金融资讯搜索关键词生成器。根据标的、市场和资讯维度，"
    "生成 1-2 条精准的搜索关键词（中英文均可，优先能搜到近期新闻的表述）。"
    "只输出 JSON 数组，如 [\"query1\", \"query2\"]，"
    "不要 Markdown 代码块、不要解释、不要多余文字。"
)


async def generate_queries(
    llm,
    symbol: str,
    market: str,
    dimension: str,
    on_delta: _DeltaCallback | None = None,
    diag: dict | None = None,
) -> list[str]:
    """LLM 生成 1-2 条搜索关键词（astream 流式）。

    非用户可见正文，但仍走 astream（铁律 #5）；每个 token 经 on_delta 回调转发，
    供调用节点用 BaseNode.emit_thinking 包装为 thinking 事件。
    返回查询字符串列表（去空、去重、最多 4 条）。

    diag（可选 out 参数）：流式异常时写入 {"error": str, "raw_len": int}，
    让调用方能区分"LLM 挂了走兜底 query"和"LLM 正常但只生成了 1 条"——
    否则 LLM 故障会被兜底 query 完全掩盖（铁律 #9：不静默降级）。
    """
    label = DIMENSION_LABELS.get(dimension, dimension)
    now = datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    user_prompt = (
        f"当前日期：{date_str}。"
        f"市场：{market}\n标的：{symbol}\n资讯维度：{dimension}（{label}）\n"
        "请生成包含近期/最新时间语境的搜索关键词（如 'AAPL 地缘政治 2026年7月'），"
        "确保搜到的是近期时事而非历史旧闻。"
        "输出 1-2 条关键词的 JSON 数组。"
    )
    messages = [
        {"role": "system", "content": _QUERY_GEN_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    content_parts: list[str] = []
    try:
        async for delta in llm.astream(messages, temperature=0.2, timeout=60.0):
            if isinstance(delta, dict):
                # usage 字典（astream 最后一个 chunk）——跳过
                continue
            if not isinstance(delta, str):
                continue
            if delta.startswith(_THINK_PREFIX):
                # 推理模型 thinking token：剥离前缀后转发，不参与 JSON 解析
                text = delta[len(_THINK_PREFIX):]
                if text and on_delta is not None:
                    await on_delta(text)
            else:
                # content token 仅累积解析；on_delta 只承载 reasoning（供节点 emit_thinking
                # "reasoning" phase），content 解析为 queries/sentiment 后经 NEWS_ITEM 事件呈现
                content_parts.append(delta)
    except Exception as exc:
        # 铁律 #9：异常不吞；用已累积的部分尽力解析，同时把故障暴露给调用方
        logger.warning(
            "generate_queries 流式失败 | symbol=%s dim=%s | err=%s",
            symbol, dimension, exc,
        )
        if diag is not None:
            diag["error"] = str(exc)

    raw = "".join(content_parts)
    if diag is not None:
        diag["raw_len"] = len(raw)
    queries = _parse_query_list(raw)
    if not queries:
        # 兜底：用维度标签拼一条，保证不空（不阻塞后续搜索）；附当前月份增强实时性
        fallback = f"{symbol} {label} {now.strftime('%Y年%m月')}"
        queries = [fallback]
        logger.info("generate_queries 解析为空，兜底 query='%s' | dim=%s", fallback, dimension)
    return queries


def _parse_query_list(raw: str) -> list[str]:
    """容错解析 LLM 输出的 JSON 数组（沿用 analyze_node._parse_analysis_json 的思路）。"""
    if not raw:
        return []

    def _from(obj) -> list[str]:
        if isinstance(obj, list):
            qs = [str(x).strip() for x in obj if str(x).strip()]
            # 去重保序 + 最多 4 条
            seen: set[str] = set()
            out: list[str] = []
            for q in qs:
                if q not in seen:
                    seen.add(q)
                    out.append(q)
            return out[:4]
        return []

    try:
        return _from(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass

    start = raw.find("[")
    end = raw.rfind("]")
    if 0 <= start < end:
        try:
            return _from(json.loads(raw[start : end + 1]))
        except (json.JSONDecodeError, TypeError):
            pass
    return []


# ══════════════════════════════════════════════════════════════════════════════
# 3. LLM 情绪打分（astream，token 经 on_delta 回调转发）
# ══════════════════════════════════════════════════════════════════════════════

_SENTIMENT_SYSTEM = (
    "你是金融资讯情绪分析器。基于给定的维度资讯（综合答案 + 多条原文），"
    "判断该维度对标的未来走势的情绪倾向。"
    '只输出 JSON：{"sentiment": -1到1的浮点（-1=极度看空 / 1=极度看多 / 0=中性），'
    '"reason": "一句简短理由"}，不要 Markdown 代码块或多余文字。'
)


async def score_sentiment(
    llm,
    dimension_result: dict,
    on_delta: _DeltaCallback | None = None,
    diag: dict | None = None,
) -> tuple[float, str]:
    """LLM 对单维度资讯打情绪分（astream 流式）。

    入参 dimension_result 形如 NewsDimensionResult（含 answer + results[...].full_content）。
    返回 (sentiment[-1,1], reason)。解析容错（复用 analyze_node 的 find-{...} 思路）。

    diag（可选 out 参数）：写入 {"error": str（仅流式异常时）, "raw_len": int}。
    调用方据此区分「LLM 打分失败」与「LLM 判定为中性」—— 二者都返回 0.0，
    但前者绝不能当成中性喂给蒙特卡洛漂移修正（那是把故障伪装成信号）。
    """
    answer = dimension_result.get("answer", "") or ""
    results = dimension_result.get("results", []) or []
    dimension = dimension_result.get("dimension", "")

    # 取 top 结果的完整原文喂给 LLM（全量保留在存储层；评分取 top 3 控制 token）
    items_text: list[str] = []
    for r in results[:3]:
        title = r.get("title", "")
        full = r.get("full_content", "") or r.get("snippet", "")
        items_text.append(f"[{title}] {full}")

    user_prompt = (
        f"资讯维度：{dimension}\n"
        f"综合答案：{answer}\n"
        f"资讯原文（Top{len(items_text)}）：\n"
        + ("\n---\n".join(items_text) if items_text else "（无原文）")
        + "\n请输出 JSON。"
    )
    messages = [
        {"role": "system", "content": _SENTIMENT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    content_parts: list[str] = []
    try:
        async for delta in llm.astream(messages, temperature=0.2, timeout=90.0):
            if isinstance(delta, dict):
                continue
            if not isinstance(delta, str):
                continue
            if delta.startswith(_THINK_PREFIX):
                text = delta[len(_THINK_PREFIX):]
                if text and on_delta is not None:
                    await on_delta(text)
            else:
                # content token 仅累积解析；on_delta 只承载 reasoning（供节点 emit_thinking
                # "reasoning" phase），content 解析为 queries/sentiment 后经 NEWS_ITEM 事件呈现
                content_parts.append(delta)
    except Exception as exc:
        # 铁律 #9：异常不吞；用部分内容尽力解析，同时把故障暴露给调用方
        logger.warning(
            "score_sentiment 流式失败 | dim=%s | err=%s", dimension, exc,
        )
        if diag is not None:
            diag["error"] = str(exc)

    raw = "".join(content_parts)
    if diag is not None:
        diag["raw_len"] = len(raw)
    sentiment, reason = _parse_sentiment_json(raw)
    logger.info(
        "score_sentiment 完成 | dim=%s | sentiment=%.3f | reason=%s",
        dimension, sentiment, reason,
    )
    return sentiment, reason


def _parse_sentiment_json(raw: str) -> tuple[float, str]:
    """容错解析 {sentiment, reason}（沿用 analyze_node._parse_analysis_json 思路）。"""
    def _extract(obj) -> tuple[float, str]:
        if not isinstance(obj, dict):
            return 0.0, ""
        try:
            s = float(obj.get("sentiment", 0))
        except (TypeError, ValueError):
            s = 0.0
        # 钳制到 [-1, 1]
        s = max(-1.0, min(1.0, s))
        reason = str(obj.get("reason", "")).strip()
        return s, reason

    if raw:
        try:
            return _extract(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if 0 <= start < end:
            try:
                return _extract(json.loads(raw[start : end + 1]))
            except (json.JSONDecodeError, TypeError):
                pass
    return 0.0, ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. 编排：12 维并发搜索 + 进度上报 + 情绪聚合
# ══════════════════════════════════════════════════════════════════════════════

def build_progress(
    label: str,
    index: int,
    total: int,
    detail: str,
    dimension: str = "",
) -> dict:
    """构造契约 F 的进度 payload（phase 固定为 news_searching）。

    节点把它同时用于两处：
      - SSE：{"event": SSEEventType.PREDICTION_PROGRESS.value, **payload}（铁律 #3）
      - DB ：update_prediction_progress(snapshot_id, payload)（刷新后秒级重建进度）
    """
    return {
        "phase": PREDICTION_PHASE_NEWS,
        "label": label,
        "index": index,
        "total": total,
        "detail": detail,
        "dimension": dimension,   # 机器可读维度 key（前端按 key 匹配，label 仅供展示）
    }


async def _safe_callback(cb, payload, what: str) -> None:
    """回调失败不能拖垮搜索（铁律 #9：至少 warning）。"""
    if cb is None:
        return
    try:
        await cb(payload)
    except Exception as exc:
        logger.warning("news_search %s 回调失败: %s", what, exc)


def _placeholder_dim(dim: str, error: str) -> dict:
    """未真正跑起来的维度占位（预算耗尽 / 未配置）——标 failed，不是"中性"。"""
    return {
        "dimension": dim,
        "query": "",
        "answer": "",
        "results": [],
        "sentiment": 0.0,
        # 失败原因同时写进 sentiment_reason：analyze_node 只把 dimension/sentiment/
        # sentiment_reason 三个字段喂给 LLM，failed/error 它看不到。
        "sentiment_reason": f"搜索失败：{error}",
        "failed": True,
        "error": error,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4bis. 数据型维度（kind="data"）：反射调 quant.data.* → 三层判定 → FilingDimensionResult
#       设计文档 §4.1「编排层直调，不上 ToolNode」：调哪个函数是**确定映射**
#       （Dimension.fetcher），用户已经在多选框里勾好了，不需要模型再决策一遍。
#       用户的真实诉求是「看到工具被调用」—— 由 TOOL_TRACE 事件 100% 满足。
# ══════════════════════════════════════════════════════════════════════════════

# 单维度最多推多少张 DATA_ITEM 卡片（全量历史公告可能有 1700+ 条，全推会把 SSE 和
# event_log 撑爆）。有负面分/非中性的条目优先，其余按日期倒序补齐。
DATA_ITEM_MAX_PER_DIM: int = 20


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fetcher_kwargs(fn, market: str, months: int) -> dict:
    """按 fetcher 的真实签名装参 —— A 股的 fetch_* 收 market/months/classify，
    SEC 的 fetch_sec_events 只收 months（多传一个 market 就 TypeError）。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError) as exc:
        logger.warning("无法反射 fetcher 签名 %r: %s", fn, exc)
        return {}
    kwargs: dict = {}
    if "market" in params:
        kwargs["market"] = market
    if "months" in params:
        kwargs["months"] = months
    if "classify" in params:
        kwargs["classify"] = True     # fetch_stock_notices：让它顺手带上规则判定字段
    return kwargs


def _normalize_fetched(payload) -> tuple[list[dict], int]:
    """fetcher 返回值 → (条目列表, 原始行数)。

    fetch_sec_events 返回的是**汇总 dict**（{negative, placements, reductions, filings_total…}），
    其余 fetch_* 返回 list[dict]。这里统一拍平，别让下游各写一份 if。
    """
    if isinstance(payload, dict):
        items = [
            *(payload.get("negative") or []),
            *(payload.get("placements") or []),
            *(payload.get("reductions") or []),
        ]
        rows = int(payload.get("filings_total") or len(items))
        return [it for it in items if isinstance(it, dict)], rows
    if isinstance(payload, list):
        items = [it for it in payload if isinstance(it, dict)]
        return items, len(items)
    logger.warning("fetcher 返回了非 list/dict：%r", type(payload))
    return [], 0


def _filing_placeholder(dim: str, fetcher: str, error: str) -> dict:
    """数据型维度的失败占位（domain.FilingDimensionResult 形状）——标 failed，不是"没风险"。

    把「数据源挂了」说成「这只票很干净」是最危险的谎（quant/data/announcement.py 头部同款告诫）。
    """
    return {
        "dimension": dim,
        "kind": "data",
        "items": [],
        "score": 0.0,
        "summary": f"取数失败：{error}",
        "fetcher": fetcher,
        "total_fetched": 0,
        "candidates": 0,
        "llm_judged": 0,
        "cache_hits": 0,
        "elapsed_ms": 0,
        "source_note": "",
        "failed": True,
        "error": error,
    }


def _pick_data_cards(items: list[dict], limit: int = DATA_ITEM_MAX_PER_DIM) -> list[dict]:
    """挑要推给前端的卡片：有负面分的优先（按严重度），再按日期倒序补齐。"""
    scored = [it for it in items if float(it.get("negative_score") or 0.0) < 0]
    scored.sort(key=lambda x: float(x.get("negative_score") or 0.0))
    picked: list[dict] = scored[:limit]
    if len(picked) < limit:
        seen = {id(x) for x in picked}
        rest = [x for x in items if id(x) not in seen]
        picked.extend(rest[: limit - len(picked)])
    return picked


async def _run_data_dimension(
    llm,
    symbol: str,
    market: str,
    dim: str,
    prediction_id: str,
    *,
    total: int,
    counter: dict,
    sem: asyncio.Semaphore,
    on_progress: _ProgressCallback | None,
    on_thinking: _DeltaCallback | None,
    on_dimension: Callable[[dict], Awaitable[None]] | None,
    on_data_item: _ItemCallback | None = None,
    on_tool_trace: _ItemCallback | None = None,
    months: int = PREDICTION_ANNOUNCE_MONTHS,
    siblings: Sequence[str] = (),
) -> dict:
    """单个数据型维度：TOOL_TRACE(start) → 反射调 fetcher → TOOL_TRACE(end)
    → announcements 三层判定 → DATA_ITEM ×N → FilingDimensionResult。

    永不抛（除 CancelledError）：数据源故障收敛为 failed 维度，其余维度继续跑
    （维度级降级，设计文档开放问题 #4）。**不消耗 Tavily 预算。**
    """
    meta = get_dimension(dim)
    label = DIMENSION_LABELS.get(dim, dim)
    fetcher_name = (meta.fetcher if meta else "") or ""

    # 单维 thinking 缓冲（与搜索型维度同款：并发下不能逐 token 直推）
    think_parts: list[str] = []
    think_len = 0

    async def _collect_thinking(text: str) -> None:
        nonlocal think_len
        if not text or think_len >= PREDICTION_NEWS_THINKING_MAX_CHARS:
            return
        think_parts.append(text)
        think_len += len(text)

    async def _flush_thinking() -> None:
        if on_thinking is None or not think_parts:
            return
        body = "".join(think_parts)
        if think_len >= PREDICTION_NEWS_THINKING_MAX_CHARS:
            body += "…（thinking 已截断）"
        try:
            await on_thinking(f"【{label}】\n{body}\n\n")
        except Exception as exc:
            logger.warning("data_dimension thinking 推送失败 | dim=%s: %s", dim, exc)

    async def _finish(result: dict) -> dict:
        await _flush_thinking()
        counter["done"] += 1
        await _safe_callback(on_dimension, result, "on_dimension")
        if result.get("failed"):
            detail = f"取数失败：{label}（{result.get('error', '')}）"
        else:
            detail = f"完成：{label}（{result.get('total_fetched', 0)} 条记录）"
        await _safe_callback(
            on_progress,
            build_progress(label, counter["done"], total, detail, dim),
            "on_progress",
        )
        return result

    async with sem:
        # ── 开跑前发进度（与搜索型维度一致：填满 HTTP 往返期间的 SSE 空洞）─────
        await _safe_callback(
            on_progress,
            build_progress(label, counter["done"], total, f"正在获取：{label}", dim),
            "on_progress",
        )

        # ── 反射解析 fetcher（Dimension.fetcher → quant.data.*）────────────────
        if not fetcher_name:
            return await _finish(_filing_placeholder(dim, "", "该维度未配置取数函数（fetcher）"))
        try:
            import quant.data as quant_data          # 惰性 import：pandas/httpx 不进启动路径
        except Exception as exc:
            logger.warning("quant.data 导入失败 | dim=%s: %s", dim, exc, exc_info=True)
            return await _finish(_filing_placeholder(dim, fetcher_name, f"数据层不可用：{exc}"))

        fn = getattr(quant_data, fetcher_name, None)
        if fn is None or not callable(fn):
            # lockup_buyback 的 fetch_lockup_buyback 目前就走这里 —— 维度级降级，
            # 明确告诉用户「未实现」，而不是返回一个空空如也的"没风险"（铁律 #9）
            logger.warning("quant.data 无取数函数 %s | dim=%s", fetcher_name, dim)
            return await _finish(
                _filing_placeholder(dim, fetcher_name, f"取数函数 {fetcher_name} 未实现")
            )

        call_kwargs = _fetcher_kwargs(fn, market, months)
        args = {"symbol": symbol, **call_kwargs}
        trace_id = f"{prediction_id}:{dim}:{fetcher_name}"
        t0 = time.perf_counter()

        # ── TOOL_TRACE（前）：「正在调用 fetch_stock_notices(000001.SZ)」 ────────
        await _safe_callback(
            on_tool_trace,
            {
                "id": trace_id,
                "phase": "start",
                "dimension": dim,
                "label": label,
                "tool": fetcher_name,
                "args": args,
                "ts": _now_ms(),
            },
            "on_tool_trace",
        )

        try:
            payload = await fn(symbol, **call_kwargs)
        except asyncio.CancelledError:
            raise                       # 用户 stop / 暂停：原样传播，不收敛成 failed
        except Exception as exc:
            # DATA_SOURCE_ERRORS（AnnouncementDataError / SecEdgarError）与意外异常同样处理：
            # 该维标 failed + 展示原因，其余维度继续（铁律 #9）
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "数据型维度取数失败 | pred=%s | dim=%s | tool=%s | %s",
                prediction_id, dim, fetcher_name, exc, exc_info=True,
            )
            await _safe_callback(
                on_tool_trace,
                {
                    "id": trace_id,
                    "phase": "end",
                    "dimension": dim,
                    "label": label,
                    "tool": fetcher_name,
                    "args": args,
                    "rows": 0,
                    "elapsed_ms": elapsed_ms,
                    "ok": False,
                    "error": str(exc),
                    "summary": f"调用失败：{exc}",
                    "ts": _now_ms(),
                },
                "on_tool_trace",
            )
            return await _finish(_filing_placeholder(dim, fetcher_name, str(exc)))

        raw_items, rows = _normalize_fetched(payload)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        # ── TOOL_TRACE（后）：「返回 1733 条，1.9s」 ────────────────────────────
        await _safe_callback(
            on_tool_trace,
            {
                "id": trace_id,
                "phase": "end",
                "dimension": dim,
                "label": label,
                "tool": fetcher_name,
                "args": args,
                "rows": rows,
                "elapsed_ms": elapsed_ms,
                "ok": True,
                "error": "",
                "summary": f"返回 {rows} 条，{elapsed_ms / 1000:.1f}s",
                "ts": _now_ms(),
            },
            "on_tool_trace",
        )

        # ── 三层判定（规则 → 候选池 → LLM，带 announcement_cache）───────────────
        try:
            items, stats = await judge_announcements(
                symbol, market, raw_items,
                dimension=dim,
                months=months,
                llm=llm,
                on_thinking=_collect_thinking,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "数据型维度判定失败 | pred=%s | dim=%s | %s",
                prediction_id, dim, exc, exc_info=True,
            )
            result = _filing_placeholder(dim, fetcher_name, f"公告判定失败：{exc}")
            result["total_fetched"] = rows
            result["elapsed_ms"] = elapsed_ms
            return await _finish(result)

        score, summary = compute_dimension_score(dim, items, siblings=siblings)
        note = source_note(market)

        # ── DATA_ITEM：每张数据卡片一条（与 NEWS_ITEM 平级）─────────────────────
        cards = _pick_data_cards(items)
        for i, card in enumerate(cards, start=1):
            await _safe_callback(
                on_data_item,
                {
                    "dimension": dim,
                    "label": label,
                    "kind": "data",
                    "fetcher": fetcher_name,
                    "index": i,
                    "total": len(cards),
                    "source_note": note,
                    "item": card,
                },
                "on_data_item",
            )

        total_elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result = {
            "dimension": dim,
            "kind": "data",
            "items": items,
            "score": score,
            "summary": summary,
            "fetcher": fetcher_name,
            "total_fetched": rows,
            "candidates": stats["candidates"],
            "llm_judged": stats["llm_judged"],
            "cache_hits": stats["cache_hits"],
            "elapsed_ms": total_elapsed_ms,
            "source_note": note,
            "failed": False,
            "error": "",
        }
        logger.info(
            "data_dimension 完成 | pred=%s | dim=%s | 拉取=%d 判定=%d（LLM=%d 缓存=%d）"
            " | score=%.3f | %dms",
            prediction_id, dim, rows, len(items),
            stats["llm_judged"], stats["cache_hits"], score, total_elapsed_ms,
        )
        return await _finish(result)


async def _run_dimension(
    llm,
    symbol: str,
    market: str,
    dim: str,
    prediction_id: str,
    *,
    total: int,
    counter: dict,
    sem: asyncio.Semaphore,
    budget_event: asyncio.Event,
    on_progress: _ProgressCallback | None,
    on_thinking: _DeltaCallback | None,
    on_dimension: Callable[[dict], Awaitable[None]] | None,
    on_data_item: _ItemCallback | None = None,
    on_tool_trace: _ItemCallback | None = None,
    months: int = PREDICTION_ANNOUNCE_MONTHS,
    siblings: Sequence[str] = (),
) -> dict:
    """单维度全流程。永不抛（异常收敛为 failed 维度）。

    ★ v2：**按 kind 分流**（第一件事）。kind="data" 的维度不碰 Tavily、不占预算，
      走 _run_data_dimension（反射调 quant.data.* → announcements 三层判定）。
      kind="search"（含未注册的 key —— 与 v1 的 DIMENSION_LABELS.get(k, k) 静默 fallback
      语义一致）走下面这条已实测跑通的 Tavily 路径。

    并发下的三个约定：
      - 进度在**拿到信号量、真正开跑那一刻**就发（不是搜完才发）。
      - thinking 按维缓冲，维度收尾时整块推送（避免 12 路 reasoning 交错成乱码）。
      - counter["done"] 单线程 event loop 内自增，无需锁。
    """
    meta = get_dimension(dim)
    if meta is not None and meta.kind == "data":
        return await _run_data_dimension(
            llm, symbol, market, dim, prediction_id,
            total=total,
            counter=counter,
            sem=sem,
            on_progress=on_progress,
            on_thinking=on_thinking,
            on_dimension=on_dimension,
            on_data_item=on_data_item,
            on_tool_trace=on_tool_trace,
            months=months,
            siblings=siblings,
        )

    label = DIMENSION_LABELS.get(dim, dim)

    # 单维 thinking 缓冲（并发下不能逐 token 直推）
    think_parts: list[str] = []
    think_len = 0

    async def _collect_thinking(text: str) -> None:
        nonlocal think_len
        if not text or think_len >= PREDICTION_NEWS_THINKING_MAX_CHARS:
            return
        think_parts.append(text)
        think_len += len(text)

    async def _flush_thinking() -> None:
        if on_thinking is None or not think_parts:
            return
        body = "".join(think_parts)
        if think_len >= PREDICTION_NEWS_THINKING_MAX_CHARS:
            body += "…（thinking 已截断）"
        try:
            await on_thinking(f"【{label}】\n{body}\n\n")
        except Exception as exc:
            logger.warning("news_search thinking 推送失败 | dim=%s: %s", dim, exc)

    async with sem:
        # ── 预算已被其他维度耗尽：不再发请求，直接占位 ──────────────────────
        if budget_event.is_set():
            result = _placeholder_dim(dim, _BUDGET_NOTE)
            counter["done"] += 1
            await _safe_callback(on_dimension, result, "on_dimension")
            await _safe_callback(
                on_progress,
                build_progress(
                    label, counter["done"], total,
                    f"跳过：{label}（搜索预算耗尽）", dim,
                ),
                "on_progress",
            )
            return result

        # ── 开跑前发进度（关键：填满 Tavily 往返期间的 SSE 空洞）─────────────
        await _safe_callback(
            on_progress,
            build_progress(label, counter["done"], total, f"正在搜索：{label}", dim),
            "on_progress",
        )

        errors: list[str] = []
        dim_results: list[dict] = []
        dim_answer = ""
        queries: list[str] = []

        try:
            # ── 4a. LLM 生成 query（astream；token 缓冲，维度收尾时整块推） ──
            qdiag: dict = {}
            queries = await generate_queries(
                llm, symbol, market, dim, on_delta=_collect_thinking, diag=qdiag,
            )
            if qdiag.get("error"):
                # LLM 挂了会走兜底 query，搜索仍能继续 —— 但不能装作无事发生
                errors.append(f"查询生成降级（{qdiag['error']}）")

            # ── 4b. 逐 query Tavily 全量搜索 ─────────────────────────────
            for q in queries:
                res = await tavily_search(q, prediction_id)
                note = res.get("note") or ""
                if note and not res.get("results"):
                    logger.info(
                        "news_search 维度查询降级 | dim=%s | query='%s' | note=%s",
                        dim, q, note,
                    )
                    errors.append(note)
                    if _BUDGET_NOTE in note:
                        # 预算是全局资源：置位后其余未开跑的维度直接占位
                        budget_event.set()
                        break
                    continue   # 单条 query 失败 → 试下一条（串行版这里是 break）
                dim_results.extend(res.get("results") or [])
                if res.get("answer"):
                    dim_answer = res["answer"]

            # ── 4c. 组装 NewsDimensionResult ─────────────────────────────
            dim_result = {
                "dimension": dim,
                "query": " | ".join(queries),
                "answer": dim_answer,
                "results": dim_results,
                "sentiment": 0.0,
                "sentiment_reason": "",
                "failed": False,
                "error": "",
            }

            has_data = bool(dim_results or dim_answer)

            if not has_data and errors:
                # 一条数据都没拿到且有报错 → 该维搜索失败（不是"中性"）
                reason = "；".join(dict.fromkeys(errors))
                dim_result["failed"] = True
                dim_result["error"] = reason
                dim_result["sentiment_reason"] = f"搜索失败：{reason}"
            elif not has_data:
                # 真·无相关资讯（Tavily 正常返回空）→ 无信号，中性且计入均值
                dim_result["sentiment_reason"] = "未检索到相关资讯"
            else:
                # ── 4d. LLM 情绪打分（astream） ──────────────────────────
                sdiag: dict = {}
                sentiment, reason = await score_sentiment(
                    llm, dim_result, on_delta=_collect_thinking, diag=sdiag,
                )
                if sdiag.get("error") and not sdiag.get("raw_len"):
                    # 打分 LLM 挂了、一个 token 都没吐出来 → 0.0 是故障不是中性
                    err = f"情绪打分失败（{sdiag['error']}）"
                    dim_result["failed"] = True
                    dim_result["error"] = err
                    dim_result["sentiment_reason"] = f"搜索失败：{err}"
                else:
                    dim_result["sentiment"] = sentiment
                    dim_result["sentiment_reason"] = reason

        except asyncio.CancelledError:
            # 用户 stop / 图取消：必须原样传播，不能收敛成 failed 维度
            raise
        except Exception as exc:
            # 铁律 #9：异常不吞。单维意外异常收敛为 failed 维度，不连累其余 11 维
            logger.warning(
                "news_search 维度异常 | pred=%s | dim=%s | %s",
                prediction_id, dim, exc, exc_info=True,
            )
            dim_result = _placeholder_dim(dim, str(exc))
            dim_result["query"] = " | ".join(queries)

        # ── 4e. 收尾：thinking 整块推 → NEWS_ITEM → 完成进度 ────────────────
        await _flush_thinking()
        counter["done"] += 1
        await _safe_callback(on_dimension, dim_result, "on_dimension")

        if dim_result["failed"]:
            detail = f"搜索失败：{label}（{dim_result['error']}）"
        else:
            detail = f"完成：{label}（{len(dim_result['results'])} 条资讯）"
        await _safe_callback(
            on_progress,
            build_progress(label, counter["done"], total, detail, dim),
            "on_progress",
        )
        return dim_result


def _is_data_dim(dim: str) -> bool:
    """未注册的 key 一律按搜索型处理（与 v1 的 DIMENSION_LABELS.get(k, k) 静默 fallback 一致）。"""
    meta = get_dimension(dim)
    return meta is not None and meta.kind == "data"


async def _skipped_search_dim(
    dim: str,
    note: str,
    *,
    total: int,
    counter: dict,
    on_progress: _ProgressCallback | None,
    on_dimension: Callable[[dict], Awaitable[None]] | None,
) -> dict:
    """搜索型维度被确定性跳过（TAVILY_API_KEY 未配置）——标 failed，不是"中性"。"""
    result = _placeholder_dim(dim, note)
    counter["done"] += 1
    await _safe_callback(on_dimension, result, "on_dimension")
    label = DIMENSION_LABELS.get(dim, dim)
    await _safe_callback(
        on_progress,
        build_progress(label, counter["done"], total, f"跳过：{label}（{note}）", dim),
        "on_progress",
    )
    return result


async def gather_all_dimensions(
    symbol: str,
    market: str,
    prediction_id: str,
    dimensions: list[str] | None = None,
    *,
    on_dimension: Callable[[dict], Awaitable[None]] | None = None,
    on_thinking: _DeltaCallback | None = None,
    on_progress: _ProgressCallback | None = None,
    on_data_item: _ItemCallback | None = None,
    on_tool_trace: _ItemCallback | None = None,
    concurrency: int = PREDICTION_NEWS_CONCURRENCY,
    months: int = PREDICTION_ANNOUNCE_MONTHS,
) -> dict:
    """v2 主入口：搜索型 + 数据型维度在**同一个 gather** 里并发（设计文档 §3.3）。

    Args:
        symbol / market / prediction_id: 标的、市场、快照 ID（Tavily 预算隔离 key）
        dimensions:   要跑的维度 key 列表；None → NEWS_DIMENSIONS（v1 的 12 个搜索维度）
        on_dimension: 单维度完成回调。**节点按 `d.get("kind") == "data"` 分流**：
                        data → SSEEventType.DATA_ITEM + append_prediction_dimension(kind="data")
                        其余 → SSEEventType.NEWS_ITEM + append_prediction_dimension(kind="search")
        on_data_item: 单张数据卡片回调（→ SSEEventType.DATA_ITEM）
        on_tool_trace: 取数工具轨迹回调（→ SSEEventType.TOOL_TRACE），每次调用前后各一条
        on_thinking:  thinking 回调（按维缓冲后整块回调一次）
        on_progress:  进度回调（每维**开跑前** + 完成后各一次）
        months:       数据型维度的回溯窗口（默认 PREDICTION_ANNOUNCE_MONTHS=24）

    Returns:
        {
          "news_digest":    list[NewsDimensionResult dict],   # 顺序跟随入参里的搜索型维度
          "news_sentiment": float,                            # 非失败搜索维度的等权均值
          "filing_digest":  list[FilingDimensionResult dict], # 数据型维度（带 kind="data"）
        }

    Raises:
        NewsSearchUnavailable: **所有真正跑过的维度**都失败（网络全挂）。
            TAVILY_API_KEY 未配置属于确定性配置问题（不是瞬时故障），走降级不 raise ——
            与 v1 行为一致。
    """
    dims = list(dimensions or NEWS_DIMENSIONS)
    total = len(dims)
    data_dims = [d for d in dims if _is_data_dim(d)]
    search_dims = [d for d in dims if d not in data_dims]

    # ── TAVILY_API_KEY 未配置：搜索型维度全部标 failed（不 raise），数据型照跑 ──
    # 配置缺失是确定性的部署问题：仍让预测跑完技术面 + 公告，但每一个搜索维度都
    # 明确标 failed + 原因，前端与 analyze LLM 都能看到「资讯缺席」。
    tavily_ok = bool(TAVILY_API_KEY)
    if not tavily_ok and search_dims:
        logger.error(
            "TAVILY_API_KEY 未配置，%d 个搜索维度全量跳过 | pred=%s | symbol=%s",
            len(search_dims), prediction_id, symbol,
        )

    if not tavily_ok and not data_dims:
        # v1 的老路径：一个维度都跑不了，直接降级返回（保持 T-PRED-NEWS-12 行为）
        digest = [_placeholder_dim(d, "TAVILY_API_KEY 未配置") for d in dims]
        for item in digest:
            await _safe_callback(on_dimension, item, "on_dimension")
        await _safe_callback(
            on_progress,
            build_progress("", total, total, "资讯搜索跳过：TAVILY_API_KEY 未配置"),
            "on_progress",
        )
        return {"news_digest": digest, "news_sentiment": 0.0, "filing_digest": []}

    llm = get_chat_llm(CHAT_MODEL, temperature=0.2)

    # 同一个 Semaphore 保护两类维度：数据型也会打东财/SEC，一样要限流
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    budget_event = asyncio.Event()      # 只对搜索型维度有意义（数据型不占 Tavily 预算）
    counter: dict = {"done": 0}

    logger.info(
        "gather_all_dimensions 启动 | pred=%s | symbol=%s | 搜索=%d 数据=%d | concurrency=%d",
        prediction_id, symbol, len(search_dims), len(data_dims), concurrency,
    )

    def _task(dim: str):
        if not tavily_ok and not _is_data_dim(dim):
            return _skipped_search_dim(
                dim, "TAVILY_API_KEY 未配置",
                total=total, counter=counter,
                on_progress=on_progress, on_dimension=on_dimension,
            )
        return _run_dimension(
            llm, symbol, market, dim, prediction_id,
            total=total,
            counter=counter,
            sem=sem,
            budget_event=budget_event,
            on_progress=on_progress,
            on_thinking=on_thinking,
            on_dimension=on_dimension,
            on_data_item=on_data_item,
            on_tool_trace=on_tool_trace,
            months=months,
            siblings=data_dims,        # sec_filings 据此判断要不要自己算分（防双重计数）
        )

    results = await asyncio.gather(
        *(_task(dim) for dim in dims),
        return_exceptions=True,   # 单维炸了不连坐；gather 保序 → digest 顺序 == dims 顺序
    )

    news_digest: list[dict] = []
    filing_digest: list[dict] = []
    for dim, res in zip(dims, results):
        if isinstance(res, BaseException):
            # _run_dimension / _run_data_dimension 内部已收敛异常，走到这里说明它自己炸了
            logger.warning(
                "维度任务异常逃逸 | pred=%s | dim=%s | %s", prediction_id, dim, res,
            )
            res = (
                _filing_placeholder(dim, "", str(res)) if _is_data_dim(dim)
                else _placeholder_dim(dim, str(res))
            )
        (filing_digest if res.get("kind") == "data" else news_digest).append(res)

    ok_sentiments = [float(d["sentiment"]) for d in news_digest if not d.get("failed")]
    executed = [d for d in (news_digest + filing_digest) if tavily_ok or d.get("kind") == "data"]
    failed = [d for d in executed if d.get("failed")]

    # 全维失败 → 不能拿一堆中性 0 分继续跑（那是把故障伪装成信号）
    if executed and len(failed) == len(executed):
        reasons = list(dict.fromkeys(d.get("error", "") for d in failed if d.get("error")))
        detail = "；".join(reasons[:3]) or "未知原因"
        msg = f"资讯搜索全部失败（网络/Tavily 不可用）：{detail}"
        logger.error("%s | pred=%s | symbol=%s", msg, prediction_id, symbol)
        raise NewsSearchUnavailable(msg, failures=failed)

    # 等权均值聚合（仅非失败的搜索维度）+ 钳制到 [-1,1]（防浮点漂移）
    news_sentiment = sum(ok_sentiments) / len(ok_sentiments) if ok_sentiments else 0.0
    news_sentiment = max(-1.0, min(1.0, news_sentiment))

    logger.info(
        "gather_all_dimensions 完成 | pred=%s | symbol=%s | 搜索 ok=%d/%d | 数据 ok=%d/%d "
        "| news_sentiment=%.3f | budget_exhausted=%s",
        prediction_id, symbol,
        len(ok_sentiments), len(news_digest),
        sum(1 for d in filing_digest if not d.get("failed")), len(filing_digest),
        news_sentiment, budget_event.is_set(),
    )
    return {
        "news_digest": news_digest,
        "news_sentiment": news_sentiment,
        "filing_digest": filing_digest,
    }


async def search_all_dimensions(
    symbol: str,
    market: str,
    prediction_id: str,
    dimensions: list[str] | None = None,
    on_dimension: Callable[[dict], Awaitable[None]] | None = None,
    on_thinking: _DeltaCallback | None = None,
    on_progress: _ProgressCallback | None = None,
    concurrency: int = PREDICTION_NEWS_CONCURRENCY,
) -> tuple[list[dict], float]:
    """v1 兼容入口：**只返回搜索型结果**（(news_digest, news_sentiment)）。

    老调用方（v1 的 NewsSearchNode、既有测试）签名不变、行为不变。
    v2 的节点请改调 gather_all_dimensions —— 否则传进来的数据型维度虽然会照跑，
    但它的 FilingDimensionResult 会在这里被丢掉（只走 on_dimension 回调）。
    """
    out = await gather_all_dimensions(
        symbol, market, prediction_id, dimensions,
        on_dimension=on_dimension,
        on_thinking=on_thinking,
        on_progress=on_progress,
        concurrency=concurrency,
    )
    if out["filing_digest"]:
        logger.warning(
            "search_all_dimensions 收到 %d 个数据型维度，其结果已丢弃 —— "
            "调用方应改用 gather_all_dimensions | pred=%s",
            len(out["filing_digest"]), prediction_id,
        )
    return out["news_digest"], out["news_sentiment"]
