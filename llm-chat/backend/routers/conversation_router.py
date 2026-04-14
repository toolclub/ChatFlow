"""
对话管理路由 — CRUD + 状态查询

等同于 Spring Boot 的 @RestController，只做参数提取和响应包装，
业务逻辑全部委托给 ConversationService。
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from models import CreateConversationRequest, UpdateConversationRequest
from services.conversation_service import conversation_service

router = APIRouter(prefix="/api", tags=["conversations"])


class BatchDeleteRequest(BaseModel):
    conversation_ids: list[str]


# ── 列表 / 创建 ──────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(request: Request):
    client_id = request.headers.get("X-Client-ID", "")
    convs = await conversation_service.list_conversations(client_id)
    return {"conversations": convs}


@router.post("/conversations")
async def create_conversation(req: CreateConversationRequest, request: Request):
    client_id = request.headers.get("X-Client-ID", "")
    return await conversation_service.create_conversation(
        title=req.title or "新对话",
        system_prompt=req.system_prompt or "",
        client_id=client_id,
    )


# ── 单个操作 ──────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    data = await conversation_service.get_conversation(conv_id)
    if not data:
        return {"error": "对话不存在"}
    return data


@router.patch("/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: UpdateConversationRequest):
    return await conversation_service.update_conversation(
        conv_id, title=req.title, system_prompt=req.system_prompt,
    )


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    return await conversation_service.delete_conversation(conv_id)


# ── 批量删除 ──────────────────────────────────────────────────────────────────

@router.post("/conversations/batch-delete")
async def batch_delete(req: BatchDeleteRequest):
    return await conversation_service.batch_delete_conversations(req.conversation_ids)


# ── 完整状态 ──────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}/full-state")
async def get_full_state(conv_id: str):
    return await conversation_service.get_full_state(conv_id)


@router.get("/conversations/{conv_id}/streaming-status")
async def get_streaming_status(conv_id: str):
    return await conversation_service.get_streaming_status(conv_id)


# ── 工具历史 / 产物 / 计划 / 记忆 ─────────────────────────────────────────────

@router.get("/conversations/{conv_id}/tools")
async def get_conversation_tools(conv_id: str):
    events = await conversation_service.get_tool_history(conv_id)
    return {"events": events}


@router.get("/conversations/{conv_id}/artifacts")
async def get_conversation_artifacts(conv_id: str):
    from db.artifact_store import get_artifact_meta_list
    artifacts = await get_artifact_meta_list(conv_id)
    return {"artifacts": artifacts}


@router.get("/conversations/{conv_id}/plan")
async def get_conversation_plan(conv_id: str):
    from db.plan_store import get_latest_plan_for_conv
    plan = await get_latest_plan_for_conv(conv_id)
    return {"plan": plan}


@router.get("/conversations/{conv_id}/memory")
async def get_memory_debug(conv_id: str):
    return await conversation_service.get_memory_debug(conv_id)
