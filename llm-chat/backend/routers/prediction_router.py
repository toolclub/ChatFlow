"""股票预测 REST 路由

接口划分（全量 admin 鉴权，D5 全量）：
  POST   /api/prediction/predict          创建预测任务 + 后台启动图，立即返回 id
  GET    /api/prediction/records          预测记录列表
  GET    /api/prediction/active           当前进行中的预测（刷新恢复用）
  GET    /api/prediction/dimensions       维度注册表（前端多选面板的唯一数据源，v2）
  GET    /api/prediction/{id}             读单个预测快照（全字段，含 v2 新列）
  GET    /api/prediction/{id}/stream      SSE 流式：replay event_log → drain 队列 / 重跑
  POST   /api/prediction/{id}/stop        停止（终态）
  POST   /api/prediction/{id}/pause       暂停（非终态，可续跑，v2）
  POST   /api/prediction/{id}/resume      续跑（跳过已完成维度，v2）
  DELETE /api/prediction/{id}             删除（含 event_log，v2）

设计（镜像 chat resume 模式）：
  - /predict 落 queued 占位行 + 以后台任务启动预测图（fire-and-forget，survives disconnect）。
  - /stream 只读：replay event_log（after_event_id 之后）→ drain 实时队列 / 重跑。
    刷新页面（断开重连）不杀图、不丢部分状态。
  - admin 鉴权是 key-only（D1）：client_id 取 X-Client-ID header（无则 "admin"），user_id=""。

★ 路由顺序注意：/dimensions 必须声明在 /{prediction_id} **之前** —— FastAPI 按声明顺序匹配，
  否则 GET /api/prediction/dimensions 会被动态段吃掉，变成"查 id=dimensions 的快照" → 404。
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse

from db.prediction_store import (
    delete_prediction,
    get_active_prediction,
    get_prediction_snapshot,
    list_prediction_records,
    prediction_v2_fields,
    save_prediction_snapshot,
)
from fsm.sse_events import SSEEventType
from graph.prediction_agent import (
    build_initial_state,
    discard_session,
    pause_prediction,
    resume_prediction,
    start_prediction_graph,
    stop_prediction,
    stream_predict,
)
from graph.prediction.dimensions import (
    DIMENSION_GROUPS,
    default_dimensions,
    to_payload,
)
from graph.prediction.domain import PredictionCriteria
from services.auth.dependencies import AdminRequired

logger = logging.getLogger("prediction.router")

router = APIRouter(prefix="/api/prediction", tags=["prediction"])


def _client_id_from_header(x_client_id: str | None) -> str:
    """admin 路径 key-only（D1）：client_id 取 X-Client-ID，无则固定 admin 标记。"""
    return (x_client_id or "admin").strip() or "admin"


def _progress_of(snap) -> dict:
    """阶段进度快照（契约 A）。运行中形如
    {"phase":"news_searching","label":"地缘政治","index":3,"total":12,"detail":"正在搜索…"}；
    终态被编排层清空为 {}（前端 hasPredictionProgress() 据此判定"无进行中的阶段"）。
    """
    return snap.progress or {}


def _error_kind_of(snap) -> str:
    """错误类别（rate_limited/network/no_data/bad_symbol/news_unavailable）。

    prediction_snapshots 没有独立的 error_kind 列，编排层 _finalize_progress() 在落 failed 时
    把它写进 progress JSONB（刻意不带 phase，因此不会被前端当成"进行中的阶段"渲染出来）。
    这里把它提到响应顶层，前端才能区分「限流/网络不通 → 稍后重试」和「代码写错了 → 改输入」。
    """
    return (snap.progress or {}).get("error_kind", "") or ""


@router.post("/predict")
async def predict(
    criteria: PredictionCriteria,
    admin: AdminRequired,
    x_client_id: str | None = Header(None, alias="X-Client-ID"),
) -> dict:
    """创建预测任务：落 queued 占位行 + 后台启动图（fire-and-forget），立即返回 id。"""
    client_id = _client_id_from_header(x_client_id)
    snapshot_id = f"pred_{uuid.uuid4().hex[:12]}"

    try:
        await save_prediction_snapshot(
            snapshot_id=snapshot_id,
            client_id=client_id,
            criteria=criteria.model_dump(),
            market=criteria.market,
            symbol=criteria.symbol,
            status="queued",
            user_id="",
            horizon=criteria.horizon,
        )
    except Exception as exc:
        logger.exception("创建预测快照失败")
        raise HTTPException(status_code=500, detail=f"初始化预测失败：{exc}")

    # 后台启动图（survives SSE disconnect；/stream 只读 event_log + 队列）
    state = build_initial_state(
        snapshot_id=snapshot_id,
        client_id=client_id,
        user_id="",
        criteria=criteria.model_dump(),
        status="queued",
    )
    try:
        await start_prediction_graph(snapshot_id, state)
    except Exception as exc:
        # 铁律 #9：不吞；启动失败记 warning 但不阻塞 200（前端可重试 /stream 触发重跑）
        logger.warning("启动预测后台图失败 snap=%s: %s", snapshot_id, exc)

    return {
        "prediction_id": snapshot_id,
        "status": "queued",
        "criteria": criteria.model_dump(),
    }


@router.get("/records")
async def list_records(
    admin: AdminRequired,
    market: str | None = None,
    limit: int = 50,
    x_client_id: str | None = Header(None, alias="X-Client-ID"),
) -> list[dict]:
    """预测记录列表（el-table 数据源）。"""
    client_id = _client_id_from_header(x_client_id)
    rows = await list_prediction_records(
        client_id, user_id="", market=market, limit=limit,
    )
    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "market": r.market,
            "status": r.status,
            "horizon": r.horizon,
            "predicted_price": r.predicted_price,
            "predicted_range": r.predicted_range,
            "confidence": r.confidence,
            "created_at": r.created_at,
            "target_date": r.target_date,
            # 进度 / 错误类别：列表里直接显示「正在搜索：地缘政治 (3/12)」，
            # 不必为了知道"跑到哪了"去开 SSE 重放几千条 thinking
            "progress": _progress_of(r),
            "updated_at": r.updated_at,
            "error_kind": _error_kind_of(r),
            # v2：事件综合分 F（列表里直接标"有爆雷/减持"）。
            # ★ 刻意**不**在列表里展开 prediction_v2_fields：filing_digest 单维就能到 200 条，
            #   50 行记录 × 16 维 × 200 条 = 几十 MB 的 JSON，列表页会被自己撑死。
            #   要看明细走 GET /{id}（那里是全量 v2 字段）。
            "event_score": r.event_score,
        }
        for r in rows
    ]


@router.get("/active")
async def get_active(
    admin: AdminRequired,
    market: str | None = None,
    x_client_id: str | None = Header(None, alias="X-Client-ID"),
) -> dict:
    """查询当前是否有进行中的预测任务（前端刷新恢复用）。"""
    client_id = _client_id_from_header(x_client_id)
    snap = await get_active_prediction(client_id, market=market, user_id="")
    if not snap:
        return {"active": False}
    return {
        "active": True,
        "prediction_id": snap.id,
        "status": snap.status,
        "criteria": snap.criteria,
        "created_at": snap.created_at,
        # 刷新恢复时前端立刻拿到"当前在哪一步"，不必等 SSE 重放
        "progress": _progress_of(snap),
        "updated_at": snap.updated_at,
    }


@router.get("/dimensions")
async def list_dimensions(admin: AdminRequired, market: str | None = None) -> dict:
    """维度注册表（DIMENSION_REGISTRY，含 desc）——前端多选面板的唯一数据源。

    前端**零维度文案硬编码**：中文名、一句话说明、分组、默认勾选全部由这里下发，
    加一个维度只改 graph/prediction/dimensions.py 一处。

    market 过滤：cn_a 下 sec_filings 这类美股专属维度整个不出现（不是灰掉，是不存在）。
    返回 {dimensions: [...], groups: [{name, keys}], defaults: [...]}。
    """
    dims = to_payload(market)
    visible = {d["key"] for d in dims}
    groups = [
        {"name": name, "keys": [k for k in keys if k in visible]}
        for name, keys in DIMENSION_GROUPS
    ]
    return {
        "dimensions": dims,
        # 空分组（如 A 股下某组维度全被过滤掉）不下发，免得前端渲染空标题
        "groups": [g for g in groups if g["keys"]],
        "defaults": default_dimensions(market),
    }


@router.get("/{prediction_id}")
async def read_prediction(prediction_id: str, admin: AdminRequired) -> dict:
    """读单个预测快照（全字段，含 v2 新列）。

    前端 loadSnapshot() 靠这些 DB 列重建终态（公式卡片 / 预测蜡烛 / 公告打点 / 工具轨迹），
    而不是重放全部 SSE 事件 —— 超出重放窗口的硬刷新也不会白屏（设计文档 §7.4）。
    """
    snap = await get_prediction_snapshot(prediction_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="预测任务不存在")
    v2 = prediction_v2_fields(snap)
    return {
        # v2 新列：filing_digest / model_params / formula_steps / forecast_candles /
        #          event_markers / tool_traces / event_score
        **v2,
        "prediction_id": prediction_id,
        "client_id": snap.client_id,
        "criteria": snap.criteria,
        "market": snap.market,
        "symbol": snap.symbol,
        "status": snap.status,
        "horizon": snap.horizon,
        "target_date": snap.target_date,
        "predicted_price": snap.predicted_price,
        "predicted_range": snap.predicted_range,
        "confidence": snap.confidence,
        "model_name": snap.model_name,
        "news_digest": snap.news_digest,
        "news_sentiment": snap.news_sentiment,
        "forecast": {
            "dates": (snap.confidence_band or {}).get("dates", []),
            "point_forecast": [r.get("price") for r in (snap.forecast or []) if isinstance(r, dict)],
            "p10": (snap.confidence_band or {}).get("p10", []),
            "p50": (snap.confidence_band or {}).get("p50", []),
            "p90": (snap.confidence_band or {}).get("p90", []),
            "inflection_points": snap.inflection_points or [],
            "confidence": snap.confidence if snap.confidence is not None else 0.0,
            "model_name": snap.model_name or "",
            "technical_indicators": snap.technical_indicators or {},
            # v2：前端 Forecast 类型里也带这三个（图表 / 公式卡片直接从 forecast 取）
            "model_params": v2["model_params"],
            "forecast_candles": v2["forecast_candles"],
            "formula_steps": v2["formula_steps"],
        },
        "inflection_points": snap.inflection_points,
        "confidence_band": snap.confidence_band,
        "technical_indicators": snap.technical_indicators,
        "analysis": snap.analysis or "",
        "risk_notes": snap.risk_notes or [],
        "thinking_segments": snap.thinking_segments or [],
        "error_message": snap.error_message or "",
        "error_kind": _error_kind_of(snap),
        "progress": _progress_of(snap),
        "created_at": snap.created_at,
        "updated_at": snap.updated_at,
    }


@router.get("/{prediction_id}/stream")
async def stream_prediction(
    prediction_id: str,
    admin: AdminRequired,
    request: Request,
    after_event_id: int = 0,
) -> StreamingResponse:
    """SSE 流式：replay event_log → drain 队列 / 重跑。

    after_event_id: 从该 event_log id 之后开始回放（前端刷新恢复时传上次收到的最大 id；
    默认 0 = 回放全部 + 实时）。断开重连不杀后台图、不丢部分状态。

    协议（铁律 #3：事件类型来自 SSEEventType 枚举）。除 ping 外每条都带 event_log 自增 `id`，
    前端用它推进 after_event_id 游标（重连只拉增量，不再全量重放 4000+ 条 thinking）：
      data: {"event":"thinking"/"content","text":"...","id":N}  # emit_thinking 映射
      data: {"event":"news_item",...,"id":N}                    # 每维资讯
      data: {"event":"forecast","forecast":{...},"id":N}        # 预测数值
      data: {"event":"inflection","points":[...],"id":N}         # 拐点
      data: {"event":"prediction_progress","phase":"...",...}   # 阶段进度（phase/label/index/total/detail）
      data: {"event":"done",...,"id":N}                          # 完成
      data: {"event":"error","message":"...","error_kind":"..."} # 失败（kind 供前端分流处置）
      data: {"event":"ping"}                                     # 3s 心跳：不入 event_log、不带 id

    X-Accel-Buffering: no —— 关掉 Nginx 的响应缓冲，否则心跳和进度事件会被攒在缓冲区里
    一起吐出来，"填满静默空档"的效果就没了。
    """
    async def event_stream():
        try:
            async for ev in stream_predict(prediction_id, after_event_id):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("prediction SSE 异常 prediction_id=%s", prediction_id)
            err = {"event": SSEEventType.ERROR.value, "message": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{prediction_id}/stop")
async def stop_prediction_endpoint(prediction_id: str, admin: AdminRequired) -> dict:
    """停止预测：取消后台 graph_task + astream + LLM HTTP 流（不再发 token）。

    铁律 #7：状态变更经 PredictionSM（stop_prediction 内 _transition_status("stopped")）。
    返回 {ok, prediction_id, status:"stopped"}。stopped 是**终态**，不能续跑 —— 要续跑用 pause。
    """
    return await stop_prediction(prediction_id)


@router.post("/{prediction_id}/pause")
async def pause_prediction_endpoint(prediction_id: str, admin: AdminRequired) -> dict:
    """暂停预测（**非终态**，可续跑）：取消在途图任务 + 置 paused，已完成维度保留。

    诚实说明（设计文档 §7.2）：没有 LangGraph checkpointer，做不到"原地冻结再解冻"。
    暂停 = 取消 + 保留已增量落库的维度结果；cancel 可能落在某个并发维度的 HTTP 请求中间，
    那一维的在途工作会丢弃（损失上限 = 并发度 5 个维度），resume 时重跑它。

    返回 {ok, prediction_id, status:"paused", completed_dimensions, remaining_dimensions}。
    终态任务（done/failed/stopped）返回 ok=false + 原因，不抛 500。
    """
    return await pause_prediction(prediction_id)


@router.post("/{prediction_id}/resume")
async def resume_prediction_endpoint(prediction_id: str, admin: AdminRequired) -> dict:
    """从 paused 续跑：重启后台图，跳过已完成维度（按 DB 里的 news_digest/filing_digest 做差）。

    status 按剩余工作量落到 news_searching / inferring / queued（三个独立的 resume 转换事件，
    见 fsm/prediction.py）。前端拿到返回后**重新开一条 /stream**（after_event_id 传已收到的
    最大 eid，避免重放已渲染过的事件）。
    """
    return await resume_prediction(prediction_id)


@router.delete("/{prediction_id}")
async def delete_prediction_endpoint(prediction_id: str, admin: AdminRequired) -> dict:
    """删除一次预测：取消在途任务 → 删 event_log → 删 prediction_snapshots。

    ★ event_log 必须显式删：那张表上**零 FK 约束**（migrate.py 里声明的 ON DELETE CASCADE
    因 create_all 先建表而永久 no-op），指望级联只会攒下几百行孤儿事件（设计文档 §7.3）。
    announcement_cache 不删：它是按 symbol 的跨预测共享缓存，删了下次还要重新烧一遍 LLM。
    """
    try:
        await discard_session(prediction_id)
    except Exception as exc:
        logger.exception("删除前停止预测失败 prediction_id=%s", prediction_id)
        raise HTTPException(status_code=503, detail="预测仍在停止中，请稍后重试删除") from exc
    deleted = await delete_prediction(prediction_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="预测任务不存在")
    return {"ok": True, "prediction_id": prediction_id}
