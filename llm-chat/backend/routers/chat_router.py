"""
聊天路由 — 流式 SSE 对话 + 停止控制

处理：
  - POST /api/chat          — 流式对话
  - POST /api/chat/:id/stop — 停止流式输出
  - GET  /api/conversations/:id/resume — 恢复流式输出
"""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, Header, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse

from models import ChatRequest
from memory import store as memory_store
from graph import runner as graph_runner
from services import llm_provider_service
from services.auth.dependencies import CurrentUser

logger = logging.getLogger("routers.chat")

router = APIRouter(prefix="/api", tags=["chat"])

# ── 本 worker 内的停止信号 ──
_stop_events: dict[str, asyncio.Event] = {}


@router.post("/chat")
async def chat(req: ChatRequest, user: CurrentUser, request: Request):
    """
    流式对话接口（SSE）。

    SSE 事件格式：
      data: {"content": "..."}         ← 增量 token
      data: {"tool_call": {...}}        ← 工具调用
      data: {"search_item": {...}}      ← 单条搜索结果（实时追加）
      data: {"tool_result": {...}}      ← 工具完成信号
      data: {"done": true, "compressed": bool}  ← 完成信号
      data: {"stopped": true}           ← 用户主动停止
    """
    client_id = user.get("client_id", "")
    user_id = user.get("id", "")

    # 用户显式传 req.model 时原样用（界面显示与实际请求一致）；
    # 不传时用当前生效默认（管理页激活供应商 ? 快照.chat_model : .env CHAT_MODEL）
    effective_model = req.model or llm_provider_service.resolve_default_request_model()

    img_bytes = sum(len(img) for img in req.images)
    logger.info(
        "POST /api/chat | conv=%s | user=%s | client=%s | model=%s | msg_len=%d",
        req.conversation_id,
        user_id[:8] if user_id else "-",
        client_id[:8] if client_id else "-",
        effective_model,
        len(req.message),
    )

    conv = memory_store.get(req.conversation_id)
    if not conv:
        await memory_store.db_get_conversation(req.conversation_id)
        conv = memory_store.get(req.conversation_id)
    if not conv:
        conv = await memory_store.create(
            req.conversation_id,
            client_id=client_id,
            user_id=user_id
        )

    # 如果该会话已有正在进行的流，先停止
    old_event = _stop_events.get(req.conversation_id)
    if old_event:
        old_event.set()
    from graph.runner.stream import _active_sessions
    old_session = _active_sessions.get(req.conversation_id)
    if old_session and old_session._graph_task and not old_session._graph_task.done():
        old_session._graph_task.cancel()

    stop_event = asyncio.Event()
    _stop_events[req.conversation_id] = stop_event

    async def safe_stream():
        try:
            async for chunk in graph_runner.stream_response(
                conv_id=req.conversation_id,
                user_message=req.message,
                model=effective_model,
                temperature=req.temperature,
                client_id=client_id,
                user_id=user_id,
                images=req.images,
                agent_mode=req.agent_mode,
                force_plan=req.force_plan,
                stop_event=stop_event,
                file_ids=req.file_ids,
                context_refs=[{"type": r.type, "id": r.id} for r in req.context_refs],
            ):
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            if _stop_events.get(req.conversation_id) is stop_event:
                _stop_events.pop(req.conversation_id, None)

    return StreamingResponse(
        safe_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/{conv_id}/stop")
async def stop_chat(
    conv_id: str,
    user: CurrentUser,
    request: Request,
    stop_token: str = Header(None),
    timeout_ms: int = Header(30000),
):
    """
    握手机制：收到停止请求后，等待后端真正停止，
    ...
    """
    # 鉴权
    conv = await memory_store.db_get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    if user.get("id"):
        if conv.get("user_id") != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该对话")
    else:
        if conv.get("user_id") or conv.get("client_id") != user.get("client_id"):
            raise HTTPException(status_code=403, detail="无权访问该对话")

    # 获取或创建 stop event
...
@router.get("/conversations/{conv_id}/resume")
async def resume_chat(
    conv_id: str,
    user: CurrentUser,
    request: Request,
    after_event_id: int = 0,
    message_id: str = ""
):
    """
    恢复流式输出（SSE）— DB-first 版。
    ...
    """
    # 鉴权
    conv = await memory_store.db_get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    if user.get("id"):
        if conv.get("user_id") != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该对话")
    else:
        if conv.get("user_id") or conv.get("client_id") != user.get("client_id"):
            raise HTTPException(status_code=403, detail="无权访问该对话")

    from graph.runner.stream import resume_stream

    async def safe_resume():
        try:
            async for chunk in resume_stream(conv_id, after_event_id, message_id):
                if await request.is_disconnected():
                    break
                yield chunk
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        safe_resume(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
