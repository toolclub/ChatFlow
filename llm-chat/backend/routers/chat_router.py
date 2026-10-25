"""
聊天路由 — 流式 SSE 对话 + 停止控制

处理：
  - POST /api/chat          — 流式对话
  - POST /api/chat/:id/stop — 停止流式输出
  - GET  /api/conversations/:id/resume — 恢复流式输出
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Header, Request, HTTPException
from fsm.sse_events import SSEEventType
from graph.runner.utils import sse
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

    logger.info(
        "POST /api/chat | conv=%s | user=%s | client=%s | model=%s | msg_len=%d",
        req.conversation_id,
        user_id[:8] if user_id else "-",
        client_id[:8] if client_id else "-",
        effective_model,
        len(req.message),
    )

    conv_data = await memory_store.db_get_conversation(req.conversation_id)
    if conv_data:
        _assert_access(conv_data, user)
    else:
        await memory_store.create(req.conversation_id, client_id=client_id, user_id=user_id)

    # A second request must not cancel and race an already running turn.
    from graph.runner.stream import _active_sessions
    if req.conversation_id in _stop_events or req.conversation_id in _active_sessions:
        raise HTTPException(status_code=409, detail="该对话仍在生成，请恢复连接或先停止当前回复")
    try:
        from db.redis_state import is_streaming
        remote_active = await asyncio.wait_for(is_streaming(req.conversation_id), timeout=2)
    except Exception as exc:
        logger.warning("读取共享流状态失败 | conv=%s | %s", req.conversation_id, exc)
        remote_active = bool(conv_data and conv_data.get("status") == "streaming")
    if remote_active:
        raise HTTPException(status_code=409, detail="该对话仍在生成，请恢复连接或先停止当前回复")

    from services.conversation_service import conversation_service
    await conversation_service.reconcile_stream_state(req.conversation_id, conv_data)
    if not await memory_store.try_start_stream(req.conversation_id):
        raise HTTPException(status_code=409, detail="该对话仍在生成，请恢复连接或先停止当前回复")

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
    """Signal the owning worker and confirm only after durable finalization."""
    conv = await memory_store.db_get_conversation_meta(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    _assert_access(conv, user)
    from graph.runner.stream import _active_sessions
    session = _active_sessions.get(conv_id)
    event = _stop_events.get(conv_id)
    if event:
        event.set()
    if session:
        session.stop_event.set()
        if session._graph_task and not session._graph_task.done():
            session._graph_task.cancel()
    try:
        from db.redis_state import publish_stop
        await asyncio.wait_for(publish_stop(conv_id), timeout=2)
    except Exception as exc:
        logger.warning("停止信号广播失败 | conv=%s | %s", conv_id, exc)
        if not session and conv.get("status") == "streaming":
            raise HTTPException(status_code=503, detail="无法联系执行进程，请稍后重试停止")

    # Both legacy headers and the current JSON client are supported.
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="停止请求必须是有效 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="停止请求必须是 JSON 对象")
    try:
        wait_ms = max(100, min(int(body.get("timeout_ms", timeout_ms)), 30000))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="timeout_ms 必须是整数")
    token = request.headers.get("X-Stop-Token") or body.get("stop_token") or stop_token or ""

    async def confirmation():
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            state = await memory_store.db_get_conversation_meta(conv_id)
            local_done = session is None or (session._consumer_task is not None and session._consumer_task.done())
            if local_done and (not state or state.get("status") != "streaming"):
                saved = await memory_store.db_get_conversation(conv_id)
                last = next((m for m in reversed((saved or {}).get("messages", [])) if m.get("role") == "assistant"), {})
                yield sse({SSEEventType.STOP_CONFIRMED.value: True,
                           "can_continue": bool(last.get("content") or last.get("stream_buffer")),
                           "reason": "stopped", "stop_token": token})
                return
            if time.monotonic() >= deadline:
                yield sse({SSEEventType.ERROR.value: "停止尚未完成，请稍后重试", "reason": "timeout", "stop_token": token})
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(confirmation(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _assert_access(conv: dict, user: dict) -> None:
    if user.get("id"):
        allowed = conv.get("user_id") == user["id"]
    else:
        allowed = not conv.get("user_id") and bool(user.get("client_id")) and conv.get("client_id") == user.get("client_id")
    if not allowed:
        raise HTTPException(status_code=403, detail="无权访问该对话")


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
    conv = await memory_store.db_get_conversation_meta(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    _assert_access(conv, user)

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
