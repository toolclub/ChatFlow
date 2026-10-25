"""
对话管理路由 — CRUD + 状态查询

等同于 Spring Boot 的 @RestController，只做参数提取和响应包装，
业务逻辑全部委托给 ConversationService。
"""
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from models import CreateConversationRequest, UpdateConversationRequest
from services.conversation_service import conversation_service
from services.auth.dependencies import CurrentUser

router = APIRouter(prefix="/api", tags=["conversations"])


class BatchDeleteRequest(BaseModel):
    conversation_ids: list[str]


# ── 列表 / 创建 ──────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(user: CurrentUser):
    convs = await conversation_service.list_conversations(
        client_id=user.get("client_id", ""),
        user_id=user.get("id", "")
    )
    return {"conversations": convs}


@router.post("/conversations")
async def create_conversation(req: CreateConversationRequest, user: CurrentUser):
    return await conversation_service.create_conversation(
        title=req.title or "新对话",
        system_prompt=req.system_prompt or "",
        client_id=user.get("client_id", ""),
        user_id=user.get("id", ""),
    )


async def _check_conv_access(conv_id: str, user: dict, include_messages: bool = False):
    """验证用户是否有权访问该对话"""
    if include_messages:
        data = await conversation_service.get_conversation(conv_id)
    else:
        from memory.store import db_get_conversation_meta
        data = await db_get_conversation_meta(conv_id)
    if not data:
        raise HTTPException(status_code=404, detail="对话不存在")
    
    # data["user_id"] 和 data["client_id"] 在 memory_store.db_get_conversation 中返回
    if user.get("id"):
        if data.get("user_id") != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该对话")
    else:
        # 匿名用户：必须匹配 client_id 且 user_id 为空
        if data.get("user_id") or data.get("client_id") != user.get("client_id"):
            raise HTTPException(status_code=403, detail="无权访问该对话")
    return data


# ── 单个操作 ──────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, user: CurrentUser):
    return await _check_conv_access(conv_id, user, include_messages=True)


@router.patch("/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: UpdateConversationRequest, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    return await conversation_service.update_conversation(
        conv_id, title=req.title, system_prompt=req.system_prompt,
    )


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    return await conversation_service.delete_conversation(conv_id)


# ── 批量删除 ──────────────────────────────────────────────────────────────────

@router.post("/conversations/batch-delete")
async def batch_delete(req: BatchDeleteRequest, user: CurrentUser):
    # 鉴权：过滤掉不属于该用户的 ID
    allowed_ids = []
    for cid in req.conversation_ids:
        try:
            await _check_conv_access(cid, user)
            allowed_ids.append(cid)
        except Exception:
            pass
    return await conversation_service.batch_delete_conversations(allowed_ids)


# ── 完整状态 ──────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}/full-state")
async def get_full_state(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    return await conversation_service.get_full_state(conv_id)


@router.get("/conversations/{conv_id}/streaming-status")
async def get_streaming_status(conv_id: str, user: CurrentUser):
    # 这里可以略过，或者也加鉴权
    await _check_conv_access(conv_id, user)
    return await conversation_service.get_streaming_status(conv_id)


# ── 工具历史 / 产物 / 计划 / 记忆 ─────────────────────────────────────────────

@router.get("/conversations/{conv_id}/tools")
async def get_conversation_tools(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    events = await conversation_service.get_tool_history(conv_id)
    return {"events": events}


@router.get("/conversations/{conv_id}/artifacts")
async def get_conversation_artifacts(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    from db.artifact_store import get_artifact_meta_list
    artifacts = await get_artifact_meta_list(conv_id)
    return {"artifacts": artifacts}


@router.get("/conversations/{conv_id}/plan")
async def get_conversation_plan(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    from db.plan_store import get_latest_plan_for_conv
    plan = await get_latest_plan_for_conv(conv_id)
    return {"plan": plan}


@router.get("/conversations/{conv_id}/memory")
async def get_memory_debug(conv_id: str, user: CurrentUser):
    await _check_conv_access(conv_id, user)
    return await conversation_service.get_memory_debug(conv_id)
