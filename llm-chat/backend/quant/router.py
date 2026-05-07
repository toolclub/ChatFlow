"""量化模块 REST 路由

接口划分：
  GET  /api/quant/status                    探活
  GET  /api/quant/providers                 列出 provider
  POST /api/quant/providers/refresh         重新探活
  POST /api/quant/screen                    同步选股（不含 LLM，秒级返回）
  GET  /api/quant/snapshot/{id}             读单个快照
  GET  /api/quant/snapshot/{id}/analyze     SSE 流式 LLM 洞察（spec 铁律 #6）

设计：
  - /screen 仅跑因子管线，返回 snapshot_id + rows + trace（首次冷启 ~10s，缓存命中 <3s）
  - /analyze 异步消费已落库的 snapshot，流式生成 analysis / risk_notes 并回写 DB
  - 拆开后前端先看到表格，AI 洞察异步流入，避免 30-60s 的 spinner
"""
from __future__ import annotations

import json
import logging
import uuid
import asyncio

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse

from db.quant_store import get_quant_snapshot, save_quant_snapshot, get_active_quant_session
from graph.quant_agent import background_screen, stream_analyze
from quant import cache_disk
from quant.cache_warmer import get_warmer
from quant.config import QUANT_ENABLED
from quant.domain import ProviderInfo, ScreenCriteria
from quant.provider_registry import NoProviderAvailable, get_registry
from quant.service import get_service
from services.auth.dependencies import CurrentUser

logger = logging.getLogger("quant.router")

router = APIRouter(prefix="/api/quant", tags=["quant"])


def _ensure_enabled() -> None:
    if not QUANT_ENABLED:
        raise HTTPException(status_code=503, detail="量化模块未启用（QUANT_ENABLED=false）")


@router.get("/status")
async def status() -> dict:
    return {"enabled": QUANT_ENABLED}


@router.get("/providers", response_model=list[ProviderInfo])
async def list_providers() -> list[ProviderInfo]:
    _ensure_enabled()
    return get_registry().list_providers()


@router.post("/providers/refresh")
async def refresh_providers() -> dict:
    _ensure_enabled()
    await get_registry().refresh_health()
    return {"ok": True}


@router.get("/cache/status")
async def cache_status() -> dict:
    """返回磁盘缓存全景：spot/bars/index 文件数、最新日期、age、磁盘占用。

    前端 QuantView 头部用此数据展示"数据更新于 X 分钟前"。
    warming 字段为 true 时前端应显示"数据同步中"。
    """
    _ensure_enabled()
    info = await cache_disk.cache_status()
    info["warmer_running"] = get_warmer().is_running()
    # 检查是否有预热任务正在进行
    try:
        from db.redis_state import _get_redis
        r = _get_redis()
        warming = await r.get("chatflow:quant:warming")
        info["warming"] = bool(warming)
    except Exception:
        info["warming"] = False
    return info


@router.post("/cache/refresh")
async def cache_refresh(kinds: list[str] | None = None) -> dict:
    """手动触发后台预热。立即返回，刷新在 warmer 任务里异步跑。

    kinds: ["spot","bars","index","prune"]，默认全部
    """
    _ensure_enabled()
    return await get_warmer().trigger_now(kinds)


@router.get("/session/active")
async def get_active_session(
    user: CurrentUser,
    market: str | None = None,
) -> dict:
    """查询当前客户端是否有正在进行的筛选任务。供前端刷新页面后恢复状态用。"""
    _ensure_enabled()
    
    snap = await get_active_quant_session(
        client_id=user.get("client_id", ""),
        market=market,
        user_id=user.get("id", "")
    )
    if not snap:
        return {"active": False}
    
    return {
        "active": True,
        "snapshot_id": snap.id,
        "status": snap.status,
        "criteria": snap.criteria,
        "created_at": snap.created_at,
    }


@router.post("/screen")
async def screen(
    criteria: ScreenCriteria,
    user: CurrentUser,
) -> dict:
    """发起选股：立即返回 snapshot_id，后台异步计算。"""
    _ensure_enabled()
    
    client_id = user.get("client_id", "")
    user_id = user.get("id", "")
    snapshot_id = f"qs_{uuid.uuid4().hex[:12]}"
    
    # 1. 立即入库一个"计算中"的占位记录
    try:
        await save_quant_snapshot(
            snapshot_id=snapshot_id,
            client_id=client_id,
            user_id=user_id,
            criteria=criteria.model_dump(),
            status="COMPUTING",
        )
    except Exception as exc:
        logger.exception("创建初始快照失败")
        raise HTTPException(status_code=500, detail=f"初始化筛选失败：{exc}")

    # 2. 触发后台异步任务（不等待）
    asyncio.create_task(background_screen(
        snapshot_id, 
        client_id, 
        criteria.model_dump(),
        user_id=user_id
    ))

    # 3. 立即返回 ID
    return {
        "snapshot_id": snapshot_id,
        "status": "COMPUTING",
        "criteria": criteria.model_dump(),
    }


async def _check_snapshot_access(snapshot_id: str, user: dict):
    """验证用户是否有权访问该快照"""
    snap = await get_quant_snapshot(snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    
    if user.get("id"):
        if snap.user_id != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该快照")
    else:
        # 匿名用户：必须匹配 client_id 且 user_id 为空
        if snap.user_id or snap.client_id != user.get("client_id"):
            raise HTTPException(status_code=403, detail="无权访问该快照")
    return snap


@router.get("/snapshot/{snapshot_id}")
async def read_snapshot(snapshot_id: str, user: CurrentUser) -> dict:
    _ensure_enabled()
    snap = await _check_snapshot_access(snapshot_id, user)
    return {
        "snapshot_id":    snap.id,
        "client_id":      snap.client_id,
        "conversation_id": snap.conversation_id,
        "criteria":       snap.criteria,
        "rows":           snap.rows,
        "status":         snap.status,
        "provider_trace": snap.provider_trace,
        "analysis":       snap.analysis or "",
        "risk_notes":     snap.risk_notes or [],
        "thinking_segments": snap.thinking_segments or [],
        "created_at":     snap.created_at,
    }


@router.get("/stock/{symbol}/chart")
async def get_stock_chart(symbol: str, days: int = 240) -> dict:
    """获取个股 K 线图数据。"""
    _ensure_enabled()
    try:
        service = get_service()
        return await service.get_stock_chart_data(symbol, days=days)
    except Exception as exc:
        logger.exception("获取股票图表失败 symbol=%s", symbol)
        raise HTTPException(status_code=500, detail=f"获取图表失败：{exc}")


@router.get("/snapshot/{snapshot_id}/analyze")
async def analyze_snapshot(
    snapshot_id: str,
    user: CurrentUser,
    request: Request,
) -> StreamingResponse:
    """SSE 流式分析。前端 fetch ReadableStream 或 EventSource 订阅。

    协议（spec §模型思考流程，phase 与 messages.thinking_segments 同形）：
      data: {"event":"thinking","text":"..."}                        # reasoning_content
      data: {"event":"delta","text":"..."}                           # 最终内容增量
      data: {"event":"done","analysis":"...","risk_notes":[...],"thinking_segments":[...]}
      data: {"event":"error","message":"..."}
    """
    _ensure_enabled()
    snap = await _check_snapshot_access(snapshot_id, user)

    rows = snap.rows or []
    criteria = snap.criteria or {}

    async def event_stream():
        try:
            async for ev in stream_analyze(snapshot_id, criteria, rows):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("analyze SSE 异常 snapshot=%s", snapshot_id)
            err = {"event": "error", "message": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
