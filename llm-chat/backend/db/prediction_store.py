"""股票预测快照持久化（仿 db/quant_store.py）。

铁律 #1：状态从 DB 字段读，不推断文本。
铁律 #4：全 async + AsyncSessionLocal（不在同步函数 await）。
铁律 #6：状态写入前由 fsm/prediction.py PredictionSM 校验（编排层先 sm.send_event，再调本模块持久化）。
铁律 #9：异常不吞，均 logger.warning。
"""
import json
import logging
import time
from typing import Any, Iterable, Optional

from sqlalchemy import select, desc, text, update

from db.database import AsyncSessionLocal
from db.models import AnnouncementCacheModel, PredictionSnapshotModel

logger = logging.getLogger("db.prediction_store")

# 增量落库的 JSONB 数组列白名单（防 SQL 注入：列名只能从这里取，不接受调用方任意字符串）
_APPEND_COLUMNS = {
    "news_digest",       # 搜索型维度结果
    "filing_digest",     # 数据型维度结果（公告/定增/减持/SEC）
    "formula_steps",     # FORMULA_STEP 序列
    "forecast_candles",  # 逐日预测蜡烛
    "event_markers",     # K 线公告打点
    "tool_traces",       # 工具调用轨迹
}


async def save_prediction_snapshot(
    snapshot_id: str,
    client_id: str,
    criteria: dict,
    market: str = "",
    symbol: str = "",
    status: str = "queued",
    user_id: str = "",
    horizon: int = 7,
) -> None:
    """立即入库一个 queued 占位行（router 层先建行再起后台任务，铁律 #1/#4）。"""
    now = time.time()
    async with AsyncSessionLocal() as session:
        snap = PredictionSnapshotModel(
            id=snapshot_id,
            client_id=client_id,
            user_id=user_id,
            criteria=criteria,
            market=market,
            symbol=symbol,
            status=status,
            horizon=horizon,
            progress={},
            created_at=now,
            updated_at=now,
        )
        session.add(snap)
        await session.commit()


async def update_prediction_status(
    snapshot_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    """状态机转换后写库（编排层先 sm.send_event 校验，再调本函数持久化，铁律 #6）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )
        snap = result.scalars().first()
        if snap is None:
            return
        snap.status = status
        if error_message is not None:
            snap.error_message = error_message
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_progress(snapshot_id: str, progress: dict) -> None:
    """覆盖写阶段进度快照（契约 A 的 progress 结构）+ 刷新 updated_at。

    进度是纯展示辅助信息，写失败绝不能阻塞图执行 —— 异常只 logger.warning（铁律 #9）。
    """
    now = time.time()
    payload = dict(progress or {})
    if payload:
        # 空 dict 保持为空（前端以 progress 是否为空判断"有无进行中的阶段"），非空才补时间戳
        payload.setdefault("updated_at", now)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
            )
            snap = result.scalars().first()
            if snap is None:
                logger.warning("update_prediction_progress: 快照不存在 %s", snapshot_id)
                return
            snap.progress = payload
            snap.updated_at = now
            await session.commit()
    except Exception as exc:
        logger.warning("update_prediction_progress 写入失败 %s: %s", snapshot_id, exc)


async def update_prediction_news(
    snapshot_id: str,
    news_digest: list[dict],
    news_sentiment: float | None = None,
) -> None:
    """资讯搜索阶段完成后回写 news_digest / news_sentiment（独立 JSONB 字段，铁律 #2）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )
        snap = result.scalars().first()
        if snap is None:
            return
        snap.news_digest = list(news_digest or [])
        if news_sentiment is not None:
            snap.news_sentiment = news_sentiment
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_forecast(
    snapshot_id: str,
    forecast: list[dict],
    inflection_points: list[dict],
    confidence_band: dict,
    predicted_price: float | None = None,
    predicted_range: list | None = None,
    confidence: float | None = None,
    model_name: str = "",
    technical_indicators: dict | None = None,
    target_date: str | None = None,
) -> None:
    """计算引擎完成后回写预测结果字段。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )
        snap = result.scalars().first()
        if snap is None:
            return
        snap.forecast = list(forecast or [])
        snap.inflection_points = list(inflection_points or [])
        snap.confidence_band = dict(confidence_band or {})
        if predicted_price is not None:
            snap.predicted_price = predicted_price
        if predicted_range is not None:
            snap.predicted_range = list(predicted_range)
        if confidence is not None:
            snap.confidence = confidence
        if model_name:
            snap.model_name = model_name
        if technical_indicators is not None:
            snap.technical_indicators = dict(technical_indicators)
        if target_date:
            snap.target_date = target_date
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_analysis(
    snapshot_id: str,
    analysis: str,
    risk_notes: list[str],
    thinking_segments: list[dict] | None = None,
    news_sentiment: float | None = None,
) -> None:
    """LLM 综合分析完成后回写 analysis / risk_notes / thinking_segments（同 snapshot 行）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )
        snap = result.scalars().first()
        if snap is None:
            return
        snap.analysis = analysis or ""
        snap.risk_notes = list(risk_notes or [])
        if thinking_segments is not None:
            snap.thinking_segments = list(thinking_segments)
        if news_sentiment is not None:
            snap.news_sentiment = news_sentiment
        snap.updated_at = time.time()
        await session.commit()


# ── v2：增量落库（暂停/续跑 + 刷新重建的前提） ────────────────────────────────

async def _append_jsonb_item(
    snapshot_id: str,
    column: str,
    item: dict,
    dedupe_key: str | None = None,
) -> bool:
    """把一个 dict 原子地 append 进快照的某个 JSONB 数组列。

    单条 UPDATE 语句完成「按 dedupe_key 去重 + 追加」，靠 Postgres 行锁串行化 ——
    并发维度（Semaphore 5）同时写同一行也不会互相覆盖（读改写会丢更新，所以不用读改写）。
    dedupe_key 不为空时，先剔除该列里同 key 的旧元素再追加 → 天然幂等，续跑重复写不会长出重复卡片。
    """
    if column not in _APPEND_COLUMNS:
        # 列名会拼进 SQL，只允许白名单内的值（铁律 #9：不静默）
        logger.warning("_append_jsonb_item: 非法列名 %s（快照 %s）", column, snapshot_id)
        return False

    now = time.time()
    payload = json.dumps(item or {}, ensure_ascii=False)

    if dedupe_key:
        new_value = (
            f"COALESCE("
            f"  (SELECT jsonb_agg(e) FROM jsonb_array_elements({column}) e"
            f"    WHERE e->>:dedupe_key IS DISTINCT FROM CAST(:item AS jsonb)->>:dedupe_key),"
            f"  '[]'::jsonb"
            f") || jsonb_build_array(CAST(:item AS jsonb))"
        )
    else:
        new_value = f"COALESCE({column}, '[]'::jsonb) || jsonb_build_array(CAST(:item AS jsonb))"

    sql = text(
        f"UPDATE prediction_snapshots "
        f"SET {column} = {new_value}, updated_at = :now "
        f"WHERE id = :pid"
    )
    params: dict[str, Any] = {"item": payload, "now": now, "pid": snapshot_id}
    if dedupe_key:
        params["dedupe_key"] = dedupe_key

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sql, params)
            await session.commit()
            if result.rowcount == 0:
                logger.warning("_append_jsonb_item: 快照不存在 %s（列 %s）", snapshot_id, column)
                return False
            return True
    except Exception as exc:
        # 增量落库失败绝不能阻塞图执行（铁律 #9：不吞，至少 warning）
        logger.warning("_append_jsonb_item 写入失败 %s.%s: %s", snapshot_id, column, exc)
        return False


async def append_prediction_dimension(
    snapshot_id: str,
    result: dict,
    kind: str = "search",
) -> bool:
    """单个维度跑完就立刻落库（news_digest / filing_digest 按 kind 分流）。

    v1 只在全部维度跑完后 update_prediction_news 一次性写入 —— 一 cancel 库里一条都没有，
    「暂停 = 取消 + 保留已完成维度 + 续跑剩余」就无从谈起。这个函数是那个能力的唯一前提。
    去重键 = dimension，同一维度重复写只保留最后一次（续跑/重试安全）。
    """
    column = "filing_digest" if kind == "data" else "news_digest"
    return await _append_jsonb_item(snapshot_id, column, result, dedupe_key="dimension")


async def append_prediction_formula_step(snapshot_id: str, step: dict) -> bool:
    """FORMULA_STEP 增量落库（去重键 stage）——刷新后靠 DB 列重建公式面板，不靠重放事件。"""
    return await _append_jsonb_item(snapshot_id, "formula_steps", step, dedupe_key="stage")


async def append_prediction_forecast_candle(snapshot_id: str, candle: dict) -> bool:
    """FORECAST_DAY 增量落库（去重键 date）——7 根蜡烛逐根长出来，逐根落库。"""
    return await _append_jsonb_item(snapshot_id, "forecast_candles", candle, dedupe_key="date")


async def append_prediction_tool_trace(snapshot_id: str, trace: dict) -> bool:
    """TOOL_TRACE 增量落库（不去重：同一工具会被多个维度调多次，每次都是独立轨迹）。"""
    return await _append_jsonb_item(snapshot_id, "tool_traces", trace)


# ── v2：终态产物回写 ──────────────────────────────────────────────────────────

async def update_prediction_model_params(snapshot_id: str, model_params: dict) -> None:
    """计算引擎回写全参数（ARIMA/GARCH/事件分/GBM/MC/影线标定），供前端公式卡片复现推导。"""
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )).scalars().first()
        if snap is None:
            logger.warning("update_prediction_model_params: 快照不存在 %s", snapshot_id)
            return
        snap.model_params = dict(model_params or {})
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_formula_steps(snapshot_id: str, formula_steps: list[dict]) -> None:
    """整体覆盖写公式步骤（终态兜底；流式过程用 append_prediction_formula_step）。"""
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )).scalars().first()
        if snap is None:
            logger.warning("update_prediction_formula_steps: 快照不存在 %s", snapshot_id)
            return
        snap.formula_steps = list(formula_steps or [])
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_candles(snapshot_id: str, forecast_candles: list[dict]) -> None:
    """整体覆盖写预测 OHLC 蜡烛（终态兜底；流式过程用 append_prediction_forecast_candle）。"""
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )).scalars().first()
        if snap is None:
            logger.warning("update_prediction_candles: 快照不存在 %s", snapshot_id)
            return
        snap.forecast_candles = list(forecast_candles or [])
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_event_markers(
    snapshot_id: str,
    event_markers: list[dict],
    event_score: float | None = None,
) -> None:
    """K 线公告打点 + 事件综合分 F（event_score ∈ [-1, 0]）回写。"""
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )).scalars().first()
        if snap is None:
            logger.warning("update_prediction_event_markers: 快照不存在 %s", snapshot_id)
            return
        snap.event_markers = list(event_markers or [])
        if event_score is not None:
            snap.event_score = event_score
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_filings(
    snapshot_id: str,
    filing_digest: list[dict],
    event_score: float | None = None,
) -> None:
    """数据型维度整体回写（终态兜底；流式过程用 append_prediction_dimension(kind="data")）。"""
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )).scalars().first()
        if snap is None:
            logger.warning("update_prediction_filings: 快照不存在 %s", snapshot_id)
            return
        snap.filing_digest = list(filing_digest or [])
        if event_score is not None:
            snap.event_score = event_score
        snap.updated_at = time.time()
        await session.commit()


async def update_prediction_historical_bars(snapshot_id: str, chart: dict) -> None:
    """compute 阶段拿到 bars_df 后，把历史 K 线图表结构落库（覆盖写 + 刷 updated_at）。

    已完成记录点开详情时前端直接用这份数据渲染，不再现拉 K 线（美股拉雅虎易被限流卡住）。
    落库失败绝不能阻塞图执行（大不了前端回退现拉）—— 异常只 logger.warning（铁律 #9）。
    """
    now = time.time()
    payload = dict(chart or {})
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
            )
            snap = result.scalars().first()
            if snap is None:
                logger.warning("update_prediction_historical_bars: 快照不存在 %s", snapshot_id)
                return
            snap.historical_bars = payload
            snap.updated_at = now
            await session.commit()
    except Exception as exc:
        logger.warning("update_prediction_historical_bars 写入失败 %s: %s", snapshot_id, exc)


# ── v2：公告 LLM 判定缓存 ─────────────────────────────────────────────────────

def _ann_cache_row_to_dict(row: AnnouncementCacheModel) -> dict:
    return {
        "market": row.market,
        "symbol": row.symbol,
        "ann_date": row.ann_date,
        "title_hash": row.title_hash,
        "title": row.title,
        "ann_type": row.ann_type,
        "url": row.url,
        "source": row.source,
        "event_type": row.event_type,
        "negative_score": row.negative_score,
        "reason": row.reason,
        "judged_by": row.judged_by,
        "fetched_at": row.fetched_at,
    }


async def get_announcement_cache(
    market: str,
    symbol: str,
    keys: Iterable[tuple[str, str]] | None = None,
    llm_ttl_days: float | None = None,
) -> dict[tuple[str, str], dict]:
    """按 (market, symbol, ann_date, title_hash) 批量查缓存。

    keys: [(ann_date, title_hash), ...]；None = 取该股票全部缓存行。
    llm_ttl_days: 只对 judged_by="llm" 的行生效（模型升级后重打分）；
                  规则判定（judged_by="rule"）是不可变历史事实，永不过期。
    返回 {(ann_date, title_hash): row_dict}，未命中的 key 不在返回值里 → 调用方按缺失走 LLM。
    """
    key_list = [(str(d), str(h)) for d, h in (keys or [])]
    if keys is not None and not key_list:
        return {}

    try:
        async with AsyncSessionLocal() as session:
            stmt = select(AnnouncementCacheModel).where(
                AnnouncementCacheModel.market == market,
                AnnouncementCacheModel.symbol == symbol,
            )
            if key_list:
                stmt = stmt.where(
                    AnnouncementCacheModel.title_hash.in_({h for _, h in key_list})
                )
            rows = list((await session.execute(stmt)).scalars().all())
    except Exception as exc:
        logger.warning("get_announcement_cache 查询失败 %s/%s: %s", market, symbol, exc)
        return {}

    wanted = set(key_list)
    cutoff = time.time() - llm_ttl_days * 86400 if llm_ttl_days else None
    hit: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.ann_date, row.title_hash)
        if wanted and key not in wanted:
            continue
        if cutoff is not None and row.judged_by == "llm" and (row.fetched_at or 0) < cutoff:
            continue  # LLM 判定已过期 → 视为未命中，重判
        hit[key] = _ann_cache_row_to_dict(row)
    return hit


async def put_announcement_cache(items: list[dict]) -> int:
    """批量 upsert 公告判定结果（唯一键 market+symbol+ann_date+title_hash）。

    同一条公告重复判定 → 覆盖旧行并刷新 fetched_at（不是插重复行）。
    缓存写失败不能让整条预测挂掉（大不了下次重新 LLM 判一遍）—— 异常只 warning（铁律 #9）。
    """
    rows = [it for it in (items or []) if it.get("title_hash")]
    if not rows:
        return 0

    now = time.time()
    sql = text(
        "INSERT INTO announcement_cache "
        "(market, symbol, ann_date, title_hash, title, ann_type, url, source, "
        " event_type, negative_score, reason, judged_by, fetched_at) "
        "VALUES (:market, :symbol, :ann_date, :title_hash, :title, :ann_type, :url, :source, "
        " :event_type, :negative_score, :reason, :judged_by, :fetched_at) "
        "ON CONFLICT (market, symbol, ann_date, title_hash) DO UPDATE SET "
        " title = EXCLUDED.title, ann_type = EXCLUDED.ann_type, url = EXCLUDED.url, "
        " source = EXCLUDED.source, event_type = EXCLUDED.event_type, "
        " negative_score = EXCLUDED.negative_score, reason = EXCLUDED.reason, "
        " judged_by = EXCLUDED.judged_by, fetched_at = EXCLUDED.fetched_at"
    )
    params = [
        {
            "market": str(it.get("market", "")),
            "symbol": str(it.get("symbol", "")),
            "ann_date": str(it.get("ann_date", "")),
            "title_hash": str(it["title_hash"]),
            "title": str(it.get("title", "")),
            "ann_type": str(it.get("ann_type", "")),
            "url": str(it.get("url", "")),
            "source": str(it.get("source", "")),
            "event_type": str(it.get("event_type", "")),
            "negative_score": it.get("negative_score"),
            "reason": str(it.get("reason", "")),
            "judged_by": str(it.get("judged_by", "")),
            "fetched_at": float(it.get("fetched_at") or now),
        }
        for it in rows
    ]
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sql, params)
            await session.commit()
        return len(params)
    except Exception as exc:
        logger.warning("put_announcement_cache 写入失败（%d 条）: %s", len(params), exc)
        return 0


# ── v2：删除 ─────────────────────────────────────────────────────────────────

async def delete_prediction(prediction_id: str) -> bool:
    """删除一次预测：event_log + prediction_snapshots。

    ★ event_log 上零 FK 约束（migrate.py 的 CREATE TABLE IF NOT EXISTS 因 create_all 先执行而
      永久 no-op，声明的 ON DELETE CASCADE 从未落地）→ 必须显式 DELETE，不能指望级联，
      否则每删一条预测就泄漏几百行孤儿事件。
    announcement_cache 不删：它是按 symbol 的跨预测共享缓存，删了下次还要重新烧 LLM。
    """
    async with AsyncSessionLocal() as session:
        ev = await session.execute(
            text("DELETE FROM event_log WHERE conv_id = :pid"), {"pid": prediction_id}
        )
        snap = await session.execute(
            text("DELETE FROM prediction_snapshots WHERE id = :pid"), {"pid": prediction_id}
        )
        await session.commit()

    deleted = (snap.rowcount or 0) > 0
    if not deleted:
        logger.warning("delete_prediction: 快照不存在 %s（event_log 清理 %d 行）",
                       prediction_id, ev.rowcount or 0)
    else:
        logger.info("delete_prediction: 已删除 %s（event_log %d 行）", prediction_id, ev.rowcount or 0)
    return deleted


# ── v2：序列化 ───────────────────────────────────────────────────────────────

def prediction_v2_fields(snap: PredictionSnapshotModel) -> dict:
    """v2 新列的统一序列化（router 直接 **展开进响应体）。

    单一真相源：前端 loadSnapshot() 靠这些 DB 列重建终态（公式卡片 / 预测蜡烛 / 公告打点 /
    工具轨迹），而不是重放全部 SSE 事件 —— 超出重放窗口的硬刷新也不会白屏。
    """
    return {
        "filing_digest": snap.filing_digest or [],
        "model_params": snap.model_params or {},
        "formula_steps": snap.formula_steps or [],
        "forecast_candles": snap.forecast_candles or [],
        "event_markers": snap.event_markers or [],
        "tool_traces": snap.tool_traces or [],
        "event_score": snap.event_score,
        # 已完成记录的历史 K 线（图表契约结构）：非空 → 前端直接渲染跳过现拉；空 {} → 旧记录回退现拉
        "historical_bars": snap.historical_bars or {},
    }


async def get_prediction_snapshot(snapshot_id: str) -> Optional[PredictionSnapshotModel]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PredictionSnapshotModel).where(PredictionSnapshotModel.id == snapshot_id)
        )
        return result.scalars().first()


async def get_active_prediction(
    client_id: str,
    market: Optional[str] = None,
    user_id: str = "",
) -> Optional[PredictionSnapshotModel]:
    """查询该客户端最近一个进行中的预测任务（1 小时内，非终态）。供前端刷新恢复用。

    paused 也算「进行中」：它是非终态，刷新后前端要能拿到它并给出「继续」按钮
    （而不是当成已结束的历史记录）。cleanup_stale_prediction_sessions 则刻意不碰 paused。
    """
    one_hour_ago = time.time() - 3600
    async with AsyncSessionLocal() as session:
        stmt = select(PredictionSnapshotModel).where(
            PredictionSnapshotModel.created_at >= one_hour_ago,
            PredictionSnapshotModel.status.in_(
                ["queued", "news_searching", "inferring", "paused"]
            ),
        )
        if user_id:
            stmt = stmt.where(PredictionSnapshotModel.user_id == user_id)
        else:
            stmt = stmt.where(
                PredictionSnapshotModel.client_id == client_id,
                PredictionSnapshotModel.user_id == "",
            )
        if market:
            stmt = stmt.where(PredictionSnapshotModel.market == market)
        result = await session.execute(
            stmt.order_by(desc(PredictionSnapshotModel.created_at)).limit(1)
        )
        return result.scalars().first()


async def list_prediction_records(
    client_id: str,
    user_id: str = "",
    market: Optional[str] = None,
    limit: int = 50,
) -> list[PredictionSnapshotModel]:
    """预测记录列表（el-table 数据源）。优先按 user_id，匿名按 client_id。"""
    async with AsyncSessionLocal() as session:
        stmt = select(PredictionSnapshotModel)
        if user_id:
            stmt = stmt.where(PredictionSnapshotModel.user_id == user_id)
        else:
            stmt = stmt.where(
                PredictionSnapshotModel.client_id == client_id,
                PredictionSnapshotModel.user_id == "",
            )
        if market:
            stmt = stmt.where(PredictionSnapshotModel.market == market)
        result = await session.execute(
            stmt.order_by(desc(PredictionSnapshotModel.created_at)).limit(limit)
        )
        return list(result.scalars().all())


async def cleanup_stale_prediction_sessions(timeout_minutes: int | None = None) -> int:
    """系统启动时或定时调用：清理卡在 queued/news_searching/inferring 的旧任务（仿 quant cleanup）。"""
    async with AsyncSessionLocal() as session:
        stmt = update(PredictionSnapshotModel).where(
            PredictionSnapshotModel.status.in_(["queued", "news_searching", "inferring"])
        )

        if timeout_minutes is not None:
            cutoff_ts = time.time() - (timeout_minutes * 60)
            stmt = stmt.where(PredictionSnapshotModel.created_at < cutoff_ts)
            reason = f"预测任务超时（超过 {timeout_minutes} 分钟未完成）"
        else:
            reason = "服务重启，预测任务已中断"

        stmt = stmt.values(status="failed", error_message=reason, updated_at=time.time())
        result = await session.execute(stmt)
        count = result.rowcount
        await session.commit()
        return count
