"""资讯 / 数据维度节点 — 搜索型（Tavily）+ 数据型（公告/SEC）同一 gather 并发。

v2 改动（设计文档 §3.3 / §7.2）：
  1. 分流：维度按 registry.kind 分成 search / data 两类，**同一个 asyncio.gather**、
     共用 Semaphore 与进度计数器（分流本身在 news_search.gather_all_dimensions 内部完成，
     节点只按 `d["kind"] == "data"` 分派 SSE 事件与落库列）。
  2. ★ 增量落库：每维一跑完就 append_prediction_dimension(...)，而不是等 12/26 维全跑完
     才 update_prediction_news 一次。这是「暂停 = 取消 + 保留已完成维度 + 续跑剩余」的
     唯一前提 —— v1 那种一次性写法下，cancel 掉库里一条维度结果都没有，pause ≈ stop。
  3. ★ 续跑跳过已完成维度：state["resume"] 为真时，从 DB 读回 news_digest / filing_digest，
     把「已落库且未失败」的维度直接当作结果复用，只把剩余维度重新跑一遍。
     失败的维度**不算完成**（会重跑）—— 否则一次 Tavily 抖动会被永久固化成"该维无资讯"。
  4. 工具轨迹：数据型维度的每次 fetcher 调用前后各一条 TOOL_TRACE（SSE + 落库），
     前端据此演出"正在调用 fetch_reductions(...)"。

合规性（spec.md 铁律）：
  - #3 事件类型用 SSEEventType.*.value，不硬编码字符串
  - #5 DB 操作全 async
  - #6 LLM 调用走 astream（在 news_search / announcements helpers 内部）
  - #7 状态变更先走 PredictionSM.send_event，再 update_prediction_status 持久化
  - #9 异常不吞，except 至少 logger.warning + 降级置 error
  - 模型无关：gather_all_dimensions 内部 get_chat_llm(CHAT_MODEL)，不硬编码 deepseek
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.callbacks.manager import adispatch_custom_event

from config import CHAT_MODEL  # noqa: F401 — 模型无关性声明（实际 LLM 在 helper 内创建）
from db.prediction_store import (
    append_prediction_dimension,
    append_prediction_tool_trace,
    get_prediction_snapshot,
    update_prediction_filings,
    update_prediction_news,
    update_prediction_progress,
    update_prediction_status,
)
from fsm.prediction import PredictionSM
from fsm.sse_events import SSEEventType
from graph.nodes.base import BaseNode
from graph.prediction.config import NEWS_DIMENSIONS, PREDICTION_PHASE_NEWS
from graph.prediction.dimensions import (
    default_dimensions,
    validate_dimensions,
)
from graph.prediction.news_search import (
    NewsSearchUnavailable,
    build_progress,
    gather_all_dimensions,
)

if TYPE_CHECKING:
    from graph.prediction_agent import PredictState

logger = logging.getLogger("graph.nodes.prediction.news_search")

# 全维失败时写进 state 的错误类别（与契约 D 的 market_data error_kind 同一字段族，
# 供前端按类别给出不同的重试引导）
ERROR_KIND_NEWS_UNAVAILABLE = "news_unavailable"

# 数据型维度结果的判据（FilingDimensionResult 恒为 "data"；NewsDimensionResult 无该字段）
KIND_DATA = "data"


def resolve_dimensions(dimensions: list[str] | None, market: str) -> list[str]:
    """要跑的维度列表：用户勾选 → 校验 → 该市场默认集兜底。

    校验（validate_dimensions）会剔除未注册的 key 和不适用本市场的 key（如 A 股勾了
    sec_filings）—— v1 完全不校验，一个垃圾 key 会照跑一遍 Tavily 烧钱。
    """
    if dimensions:
        valid, invalid = validate_dimensions(list(dimensions), market)
        if invalid:
            logger.warning(
                "维度校验丢弃非法/不适用 key market=%s invalid=%s", market, invalid,
            )
        if valid:
            return valid
    return default_dimensions(market) or list(NEWS_DIMENSIONS)


def _done_dimensions(seed_digests: list[dict]) -> set[str]:
    """已完成 = 已落库且未失败。失败维度要重跑（不把一次抖动固化成"该维无资讯"）。"""
    return {
        str(d.get("dimension") or "")
        for d in seed_digests
        if isinstance(d, dict) and d.get("dimension") and not d.get("failed")
    }


def _mean_sentiment(news_digest: list[dict]) -> float:
    """非失败搜索维度的等权均值（与 news_search.gather_all_dimensions 同口径）。

    续跑时必须在这里重算：gather 只看得见"本轮真正跑过"的维度，算出来的均值不含
    上一轮已完成的那些维度。
    """
    vals = [
        float(d.get("sentiment") or 0.0)
        for d in news_digest
        if not d.get("failed")
    ]
    if not vals:
        return 0.0
    return max(-1.0, min(1.0, sum(vals) / len(vals)))


class NewsSearchNode(BaseNode):
    """资讯（Tavily）+ 数据（公告/SEC）维度并发节点。"""

    @property
    def name(self) -> str:
        return "predict_news"

    async def execute(self, state: "PredictState") -> dict:
        snapshot_id = state.get("snapshot_id", "")
        symbol = state.get("symbol", "")
        market = state.get("market", "cn_a")
        is_resume = bool(state.get("resume"))

        dims = resolve_dimensions(state.get("dimensions"), market)
        total = len(dims)

        # ── 铁律 #7：状态机先校验，再写 DB ──────────────────────────────────
        # 续跑时状态可能已被 resume_prediction 置成 news_searching / inferring：
        # 再 send_event("news_searching") 会撞非法转换（start_news 要求 queued，
        # resume_news 要求 paused）→ 只是记 warning 并原样返回，但日志噪音没意义。
        cur_status = state.get("status", "queued") or "queued"
        if cur_status != PREDICTION_PHASE_NEWS:
            sm = PredictionSM.from_db_status(cur_status)
            new_status = sm.send_event(PREDICTION_PHASE_NEWS)
            try:
                await update_prediction_status(snapshot_id, new_status)
            except Exception as exc:
                logger.warning(
                    "update_prediction_status(news_searching) 失败 snap=%s: %s",
                    snapshot_id, exc,
                )

        # ── 续跑：读回已完成维度，只跑剩余 ────────────────────────────────────
        seed_news: list[dict] = []
        seed_filings: list[dict] = []
        if is_resume:
            seed_news, seed_filings = await self._load_seeds(snapshot_id)

        done = _done_dimensions(seed_news) | _done_dimensions(seed_filings)
        remaining = [d for d in dims if d not in done]
        skipped = total - len(remaining)

        # ── 进度回调：SSE 事件（铁律 #3 枚举值）+ 覆盖写 DB progress ─────────
        # 只在「每维开跑前 / 每维完成后」调用（约 2×N 次），不会每 token 写库。
        async def _on_progress(payload: dict) -> None:
            try:
                await adispatch_custom_event(
                    "prediction_event",
                    {"type": SSEEventType.PREDICTION_PROGRESS.value, **payload},
                )
            except Exception as exc:
                logger.warning("dispatch prediction_progress 失败 snap=%s: %s", snapshot_id, exc)
            # update_prediction_progress 内部保证不抛（契约 B），失败只 warning
            await update_prediction_progress(snapshot_id, payload)

        async def _on_dimension(d: dict) -> None:
            """单维完成：SSE + ★增量落库（暂停续跑的前置）。"""
            is_data = d.get("kind") == KIND_DATA
            ev_type = (
                SSEEventType.DATA_ITEM.value if is_data else SSEEventType.NEWS_ITEM.value
            )
            try:
                await adispatch_custom_event("prediction_event", {"type": ev_type, **d})
            except Exception as exc:
                logger.warning("dispatch %s 失败 snap=%s: %s", ev_type, snapshot_id, exc)
            # 落库失败不阻塞图（store 内部已 warning + 返回 False）；后果只是该维要重跑
            await append_prediction_dimension(
                snapshot_id, d, kind=KIND_DATA if is_data else "search",
            )

        async def _on_data_item(card: dict) -> None:
            """单张公告卡片（DATA_ITEM，payload 带 item 单数）——不落库：整维结果
            落 filing_digest 时已包含全部 items，卡片只是流式演出。"""
            try:
                await adispatch_custom_event(
                    "prediction_event",
                    {"type": SSEEventType.DATA_ITEM.value, **card},
                )
            except Exception as exc:
                logger.warning("dispatch data_item 失败 snap=%s: %s", snapshot_id, exc)

        async def _on_tool_trace(trace: dict) -> None:
            """取数工具轨迹（start/end 成对）：SSE + 落库（刷新后能重建"调过哪些工具"）。"""
            try:
                await adispatch_custom_event(
                    "prediction_event",
                    {"type": SSEEventType.TOOL_TRACE.value, **trace},
                )
            except Exception as exc:
                logger.warning("dispatch tool_trace 失败 snap=%s: %s", snapshot_id, exc)
            await append_prediction_tool_trace(snapshot_id, trace)

        async def _on_thinking(delta: str) -> None:
            # 铁律 #7：thinking 统一经 BaseNode.emit_thinking
            # （helper 已按维缓冲，这里收到的是整块 reasoning，不是单 token）
            await BaseNode.emit_thinking(self.name, "reasoning", delta, None)

        # ── 阶段开场进度（保留 status 字段：老前端按 status 判阶段）───────────
        detail = (
            f"续跑：跳过 {skipped} 个已完成维度，剩余 {len(remaining)} 个"
            if skipped
            else f"开始搜索 {total} 个维度…"
        )
        await _on_progress({
            **build_progress("", skipped, total, detail),
            "status": PREDICTION_PHASE_NEWS,
        })

        # 全部维度都已完成（暂停发生在资讯阶段之后）→ 一次外部调用都不发，直接进 compute
        if not remaining:
            logger.info(
                "predict_news 全部 %d 维已完成（续跑跳过）snap=%s", total, snapshot_id,
            )
            news_digest = self._order(seed_news, dims)
            filing_digest = self._order(seed_filings, dims)
            return {
                **state,
                "news_digest": news_digest,
                "news_sentiment": _mean_sentiment(news_digest),
                "filing_digest": filing_digest,
                "status": PREDICTION_PHASE_NEWS,
            }

        try:
            # ── 搜索型 + 数据型维度同一 gather 并发 ─────────────────────────
            gathered = await gather_all_dimensions(
                symbol,
                market,
                snapshot_id,
                remaining,
                on_dimension=_on_dimension,
                on_thinking=_on_thinking,
                on_progress=_on_progress,
                on_data_item=_on_data_item,
                on_tool_trace=_on_tool_trace,
            )

            news_digest = self._order(
                seed_news + list(gathered.get("news_digest") or []), dims,
            )
            filing_digest = self._order(
                seed_filings + list(gathered.get("filing_digest") or []), dims,
            )
            # 续跑时不能用 gather 返回的 news_sentiment（它只统计本轮跑过的维度）
            news_sentiment = _mean_sentiment(news_digest)

            failed_count = sum(
                1 for d in (news_digest + filing_digest) if d.get("failed")
            )
            ok = total - failed_count
            await _on_progress(build_progress(
                "", total, total,
                f"维度搜索完成（{ok}/{total} 维成功）" if failed_count
                else f"维度搜索完成（{total}/{total} 维）",
            ))

            # ── 结果落库（独立 JSONB 字段，铁律 #2）──────────────────────────
            # 每维已 append 过；这里再整体覆盖一次做兜底 —— 单条 append 失败（如
            # 瞬时 DB 抖动）不至于让该维在刷新后凭空消失。
            try:
                await update_prediction_news(snapshot_id, news_digest, news_sentiment)
            except Exception as exc:
                logger.warning("update_prediction_news 失败 snap=%s: %s", snapshot_id, exc)
            if filing_digest:
                try:
                    await update_prediction_filings(snapshot_id, filing_digest)
                except Exception as exc:
                    logger.warning("update_prediction_filings 失败 snap=%s: %s", snapshot_id, exc)

            logger.info(
                "predict_news 完成 snap=%s symbol=%s dims=%d(跳过 %d) search=%d data=%d "
                "failed=%d sentiment=%.3f",
                snapshot_id, symbol, total, skipped,
                len(news_digest), len(filing_digest), failed_count, news_sentiment,
            )
            return {
                **state,
                "news_digest": news_digest,
                "news_sentiment": news_sentiment,
                "filing_digest": filing_digest,
                "status": PREDICTION_PHASE_NEWS,
            }

        except NewsSearchUnavailable as exc:
            # 本轮真正跑过的维度全失败：置 error → 图短路（契约 E 的条件边），编排层落 failed。
            # 不再拿一堆"中性 0 分"的空维度继续跑预测。
            logger.warning(
                "predict_news 全维失败 snap=%s symbol=%s: %s",
                snapshot_id, symbol, exc.message,
            )
            await _on_progress(build_progress("", total, total, exc.message))
            failures = list(exc.failures or [])
            return {
                **state,
                "news_digest": self._order(
                    seed_news + [f for f in failures if f.get("kind") != KIND_DATA], dims,
                ),
                "news_sentiment": 0.0,
                "filing_digest": self._order(
                    seed_filings + [f for f in failures if f.get("kind") == KIND_DATA], dims,
                ),
                "error": exc.message,
                "error_kind": ERROR_KIND_NEWS_UNAVAILABLE,
                "status": PREDICTION_PHASE_NEWS,
            }

        except Exception as exc:
            # 铁律 #9：异常不吞，记 warning + 降级置 error（编排层据此落 failed）
            logger.warning(
                "predict_news 维度搜索失败 snap=%s symbol=%s: %s",
                snapshot_id, symbol, exc, exc_info=True,
            )
            return {**state, "error": str(exc), "status": PREDICTION_PHASE_NEWS}

    # ── 续跑辅助 ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _load_seeds(snapshot_id: str) -> tuple[list[dict], list[dict]]:
        """读回已落库的维度结果（news_digest / filing_digest）。

        读失败 → 当作"没有已完成维度"，全量重跑（多花钱但结果正确；铁律 #9 不静默）。
        """
        try:
            snap = await get_prediction_snapshot(snapshot_id)
        except Exception as exc:
            logger.warning(
                "续跑读取已完成维度失败 snap=%s: %s（降级为全量重跑）", snapshot_id, exc,
            )
            return [], []
        if snap is None:
            return [], []
        news = [d for d in (snap.news_digest or []) if isinstance(d, dict)]
        filings = [d for d in (snap.filing_digest or []) if isinstance(d, dict)]
        logger.info(
            "续跑读回已落库维度 snap=%s search=%d data=%d", snapshot_id, len(news), len(filings),
        )
        return news, filings

    @staticmethod
    def _order(digest: list[dict], dims: list[str]) -> list[dict]:
        """按用户勾选顺序排列，并按 dimension 去重（seed 与本轮结果可能重叠，取后者）。"""
        by_dim: dict[str, dict] = {}
        for d in digest:
            key = str(d.get("dimension") or "")
            if key:
                by_dim[key] = d
        rank = {k: i for i, k in enumerate(dims)}
        return sorted(by_dim.values(), key=lambda d: rank.get(str(d.get("dimension")), 999))
