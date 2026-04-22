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

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from models import ChatRequest
from memory import store as memory_store
from graph import runner as graph_runner
from config import CHAT_MODEL

logger = logging.getLogger("routers.chat")

router = APIRouter(prefix="/api", tags=["chat"])

# ── 本 worker 内的停止信号 ──
_stop_events: dict[str, asyncio.Event] = {}


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
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
    client_id = request.headers.get("X-Client-ID", "")

    img_bytes = sum(len(img) for img in req.images)
    logger.info(
        "POST /api/chat | conv=%s | client=%s | model=%s | msg_len=%d"
        " | images=%d | img_total_kb=%.1f | files=%d",
        req.conversation_id,
        client_id[:8] if client_id else "-",
        req.model or CHAT_MODEL,
        len(req.message),
        len(req.images),
        img_bytes / 1024,
        len(req.file_ids),
    )

    conv = memory_store.get(req.conversation_id)
    if not conv:
        await memory_store.db_get_conversation(req.conversation_id)
        conv = memory_store.get(req.conversation_id)
    if not conv:
        conv = await memory_store.create(req.conversation_id, client_id=client_id)

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
                model=req.model or CHAT_MODEL,
                temperature=req.temperature,
                client_id=client_id,
                images=req.images,
                agent_mode=req.agent_mode,
                force_plan=req.force_plan,
                stop_event=stop_event,
                file_ids=req.file_ids,
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


@router.post("/api/chat/{conv_id}/stop")
async def stop_chat(
    conv_id: str,
    request: Request,
    stop_token: str = Header(None),
    timeout_ms: int = Header(30000),
):
    """
    握手机制：收到停止请求后，等待后端真正停止，
    然后通过 SSE 发送 stop_confirmed 确认事件。
    """
    # 获取或创建 stop event
    stop_event = _stop_events.get(conv_id)
    if stop_event:
        stop_event.set()

    # 取消 graph task
    from graph.runner.stream import _active_sessions
    session = _active_sessions.get(conv_id)
    if session and session._graph_task and not session._graph_task.done():
        session._graph_task.cancel()

    # 跨 worker 广播
    try:
        from db.redis_state import publish_stop
        await publish_stop(conv_id)
    except Exception as exc:
        logger.warning("Redis publish_stop 失败: %s", exc)

    # 轮询等待 session 停止
    async def event_generator():
        start = time.time()
        timeout = timeout_ms / 1000

        while time.time() - start < timeout:
            sess = _active_sessions.get(conv_id)
            if sess is None or sess._sse_done:
                break
            await asyncio.sleep(0.1)

        # 发送确认事件
        can_cont = False
        if session:
            content = getattr(session, 'best_partial', '') or ''
            can_cont = bool(content.strip())

        yield f"data: {json.dumps({
            'stop_confirmed': True,
            'stop_token': stop_token or '',
            'can_continue': can_cont,
            'reason': 'stopped',
        })}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations/{conv_id}/resume")
async def resume_chat(conv_id: str, request: Request, after_event_id: int = 0, message_id: str = ""):
    """
    恢复流式输出（SSE）— DB-first 版。

    从 event_log 表读取 after_event_id 之后的事件，
    然后切换到实时推送。跨 worker 安全。
    """
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
