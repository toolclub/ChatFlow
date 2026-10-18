"""
管理者页面路由 — 系统运行状况监控 + Token 统计 + LLM 供应商管理

职责：
  - 验证管理者密钥
  - 聚合用户访问数据
  - 统计 Token 消耗情况 (Prompt/Completion/Reasoning)
  - 提供 ECharts 所需的结构化数据
  - LLM 供应商 CRUD / 激活切换 / 连通性测试（cc-switch 模式，全部 X-Admin-Key 保护）
"""
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from sqlalchemy import select, func, desc
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from openai import AsyncOpenAI

from db.database import AsyncSessionLocal
from db.models import UserModel, ConversationModel, MessageModel, UserUsageLogModel
from db import llm_provider_store
from services import llm_provider_service
from services.auth.dependencies import CurrentUser, verify_admin_access, AdminRequired
import config

logger = logging.getLogger("routers.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

ADMIN_SECRET_KEY = config.ADMIN_SECRET_KEY

class VerifyKeyRequest(BaseModel):
    key: str

# ── 鉴权中间件（verify_admin_access 已上提到 services/auth/dependencies.py，供 quant + prediction 共用） ──

# ── 接口实现 ──────────────────────────────────────────────────────────────────

@router.post("/verify")
async def verify_key(req: VerifyKeyRequest):
    """验证管理密钥"""
    if req.key == ADMIN_SECRET_KEY:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="密钥错误")


@router.get("/stats")
async def get_system_stats(authorized: bool = Depends(verify_admin_access)):
    """获取系统统计概览（用于 ECharts）"""
    async with AsyncSessionLocal() as session:
        # 1. 基础总计 (用户和会话依然看主表)
        user_count = (await session.execute(select(func.count(UserModel.id)))).scalar() or 0
        conv_count = (await session.execute(select(func.count(ConversationModel.id)))).scalar() or 0
        # 消息总数看审计日志，不随删除减少
        msg_count = (await session.execute(
            select(func.count(UserUsageLogModel.id)).where(UserUsageLogModel.node == "call_model")
        )).scalar() or 0

        # 2. Token 总计（从 UserUsageLogModel 获取）
        token_stats = (await session.execute(
            select(
                func.sum(UserUsageLogModel.prompt_tokens).label("prompt"),
                func.sum(UserUsageLogModel.completion_tokens).label("completion"),
                func.sum(UserUsageLogModel.reasoning_tokens).label("reasoning")
            )
        )).first()

        # 2.1 用户 vs 游客 Token 消耗
        user_tokens = (await session.execute(
            select(func.sum(UserUsageLogModel.total_tokens))
            .where(UserUsageLogModel.user_id != "")
        )).scalar() or 0

        guest_tokens = (await session.execute(
            select(func.sum(UserUsageLogModel.total_tokens))
            .where(UserUsageLogModel.user_id == "")
        )).scalar() or 0

        # 3. 今日数据 (看审计日志)
        today_start = datetime.combine(datetime.today(), datetime.min.time()).timestamp()
        new_users_today = (await session.execute(
            select(func.count(UserModel.id)).where(UserModel.created_at >= today_start)
        )).scalar() or 0
        msgs_today = (await session.execute(
            select(func.count(UserUsageLogModel.id))
            .where(UserUsageLogModel.node == "call_model", UserUsageLogModel.created_at >= today_start)
        )).scalar() or 0

        # 4. 过去 7 天趋势数据 (看审计日志，保证不可变性)
        trend_days = []
        trend_msg_counts = []
        trend_token_counts = []

        for i in range(6, -1, -1):
            d = datetime.now() - timedelta(days=i)
            day_str = d.strftime("%m-%d")
            day_start = datetime.combine(d, datetime.min.time()).timestamp()
            day_end = day_start + 86400

            trend_days.append(day_str)

            # 消息数（审计日志中节点为 call_model 的记录视为一次有效对话产出）
            m_c = (await session.execute(
                select(func.count(UserUsageLogModel.id))
                .where(UserUsageLogModel.node == "call_model", 
                       UserUsageLogModel.created_at >= day_start, 
                       UserUsageLogModel.created_at < day_end)
            )).scalar() or 0
            trend_msg_counts.append(m_c)

            # Token 数
            t_c = (await session.execute(
                select(func.sum(UserUsageLogModel.total_tokens))
                .where(UserUsageLogModel.created_at >= day_start, 
                       UserUsageLogModel.created_at < day_end)
            )).scalar() or 0
            trend_token_counts.append(int(t_c))

        # 5. 模型分布 (Pie Chart)
        model_dist_result = await session.execute(
            select(UserUsageLogModel.model, func.count(UserUsageLogModel.id))
            .group_by(UserUsageLogModel.model)
        )
        model_dist = [
            {"name": r[0] or "unknown", "value": r[1]} 
            for r in model_dist_result.all()
        ]

    return {
        "summary": {
            "total_users": user_count,
            "total_conversations": conv_count,
            "total_messages": msg_count,
            "total_prompt_tokens": int(token_stats.prompt or 0),
            "total_completion_tokens": int(token_stats.completion or 0),
            "total_reasoning_tokens": int(token_stats.reasoning or 0),
            "user_tokens": int(user_tokens),
            "guest_tokens": int(guest_tokens),
            "new_users_today": new_users_today,
            "messages_today": msgs_today,
        },
        "charts": {
            "trend": {
                "days": trend_days,
                "messages": trend_msg_counts,
                "tokens": trend_token_counts,
            },
            "models": model_dist,
        }
    }



@router.get("/users")
async def get_recent_users(authorized: bool = Depends(verify_admin_access)):
    """获取最近活跃用户列表（带 Token 统计）"""
    async with AsyncSessionLocal() as session:
        # 1. 获取用户基本信息
        result = await session.execute(
            select(UserModel)
            .order_by(desc(UserModel.last_login_at))
            .limit(50)
        )
        users = result.scalars().all()

        # 2. 批量查询这些用户的 Token 消耗
        user_ids = [u.id for u in users]
        usage_result = await session.execute(
            select(
                UserUsageLogModel.user_id,
                func.sum(UserUsageLogModel.total_tokens).label("total")
            )
            .where(UserUsageLogModel.user_id.in_(user_ids))
            .group_by(UserUsageLogModel.user_id)
        )
        usage_map = {r[0]: int(r[1] or 0) for r in usage_result.all()}

    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "last_login_at": u.last_login_at,
            "created_at": u.created_at,
            "is_active": u.is_active,
            "total_tokens": usage_map.get(u.id, 0)
        }
        for u in users
    ]


# ══════════════════════════════════════════════════════════════════════════════
# LLM 供应商管理（cc-switch 模式）
#   管理员在管理页维护多个供应商配置，激活一条后全局生效；
#   无激活配置时回退 .env 默认。全部 AdminRequired（普通用户 403）。
# ══════════════════════════════════════════════════════════════════════════════


class LLMProviderCreateRequest(BaseModel):
    name: str
    base_url: str
    api_key: str
    chat_model: str
    summary_model: Optional[str] = None
    router_model: Optional[str] = None
    vision_model: Optional[str] = None
    extra_models: Optional[list[str]] = None
    remark: Optional[str] = None
    enabled: bool = True


class LLMProviderUpdateRequest(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    chat_model: Optional[str] = None
    summary_model: Optional[str] = None
    router_model: Optional[str] = None
    vision_model: Optional[str] = None
    extra_models: Optional[list[str]] = None
    remark: Optional[str] = None
    enabled: Optional[bool] = None


class LLMProviderTestRequest(BaseModel):
    """连通性测试：传 {base_url, api_key, model} 测草稿配置，或传 {id} 测已存配置。"""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    id: Optional[int] = None


class LLMProviderFetchModelsRequest(BaseModel):
    base_url: str
    api_key: str


def _provider_dict(p) -> dict:
    """ORM 行 → 响应 dict（api_key 明文仅 admin 可达，与 cc-switch 同做法）。"""
    return {
        "id": p.id,
        "name": p.name,
        "base_url": p.base_url,
        "api_key": p.api_key,
        "chat_model": p.chat_model,
        "summary_model": p.summary_model,
        "router_model": p.router_model,
        "vision_model": p.vision_model,
        "extra_models": p.extra_models or [],
        "remark": p.remark,
        "enabled": bool(p.enabled),
        "is_active": bool(p.is_active),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


async def _notify_llm_config_changed() -> None:
    """写操作后让配置变更立即生效：本 worker 重读快照 + 广播其他 worker。

    Redis/DB 异常只记日志（铁律 #9），不影响写操作本身成功返回；
    其他 worker 还有 15s 定时刷新兜底。
    """
    try:
        await llm_provider_service.refresh_from_db()
    except Exception as exc:
        logger.warning("LLM 供应商快照刷新失败（等待定时兜底）: %s", exc)
    try:
        from db.redis_state import publish_llm_config_invalidation
        await publish_llm_config_invalidation()
    except Exception as exc:
        logger.warning("LLM 配置失效广播失败（等待各 worker 定时兜底）: %s", exc)


@router.get("/llm-providers")
async def list_llm_providers(authorized: AdminRequired):
    """供应商列表 + 当前生效配置（api_key 一律不返回给 effective）。"""
    providers = await llm_provider_store.list_providers()
    snapshot = llm_provider_service.get_snapshot()
    if snapshot:
        effective = {
            "source": "db",
            "base_url": snapshot.base_url,
            "chat_model": snapshot.chat_model,
            "models": llm_provider_service.visible_models(),
        }
    else:
        effective = {
            "source": "env",
            "base_url": config.LLM_BASE_URL,
            "chat_model": config.CHAT_MODEL,
            "models": [config.CHAT_MODEL],
        }
    return {"providers": [_provider_dict(p) for p in providers], "effective": effective}


@router.post("/llm-providers")
async def create_llm_provider(req: LLMProviderCreateRequest, authorized: AdminRequired):
    """新建供应商配置；name 重复 → 409。新建的配置默认不激活。"""
    if await llm_provider_store.get_provider_by_name(req.name):
        raise HTTPException(status_code=409, detail=f"供应商名称「{req.name}」已存在")
    try:
        row = await llm_provider_store.create_provider(
            name=req.name,
            base_url=req.base_url,
            api_key=req.api_key,
            chat_model=req.chat_model,
            summary_model=req.summary_model,
            router_model=req.router_model,
            vision_model=req.vision_model,
            extra_models=req.extra_models or [],
            remark=req.remark,
            enabled=req.enabled,
            is_active=False,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"供应商名称「{req.name}」已存在")
    return {"provider": _provider_dict(row)}


@router.put("/llm-providers/{provider_id}")
async def update_llm_provider(provider_id: int, req: LLMProviderUpdateRequest, authorized: AdminRequired):
    """部分更新（只改传入的字段）；不存在 → 404，name 撞车 → 409。"""
    fields = req.model_dump(exclude_unset=True)
    if "name" in fields:
        existing = await llm_provider_store.get_provider_by_name(fields["name"])
        if existing and existing.id != provider_id:
            raise HTTPException(status_code=409, detail=f"供应商名称「{fields['name']}」已存在")
    # is_active 只能通过 activate/deactivate 端点变更，不接受 PUT 直改
    fields.pop("is_active", None)
    try:
        row = await llm_provider_store.update_provider(provider_id, **fields)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="供应商名称已存在")
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    # 激活中的配置被改动 → 生效值可能变化，广播失效
    if row.is_active:
        await _notify_llm_config_changed()
    return {"provider": _provider_dict(row)}


@router.delete("/llm-providers/{provider_id}")
async def delete_llm_provider(provider_id: int, authorized: AdminRequired):
    """删除供应商；删的是激活项 → 自动回退 .env 并广播失效。"""
    deleted = await llm_provider_store.delete_provider(provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="供应商不存在")
    await _notify_llm_config_changed()
    return {"ok": True}


@router.post("/llm-providers/deactivate")
async def deactivate_llm_providers(authorized: AdminRequired):
    """全部取消激活，恢复 .env 默认配置。"""
    await llm_provider_store.deactivate_all()
    await _notify_llm_config_changed()
    return {"ok": True}


@router.post("/llm-providers/{provider_id}/activate")
async def activate_llm_provider(provider_id: int, authorized: AdminRequired):
    """激活指定供应商（单事务内保证至多一条 is_active），立即广播生效。"""
    row = await llm_provider_store.activate_provider(provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    await _notify_llm_config_changed()
    return {"provider": _provider_dict(row)}


@router.post("/llm-providers/test")
async def test_llm_provider(req: LLMProviderTestRequest, authorized: AdminRequired):
    """
    连通性测试：发一条极小的非流式请求。

    ok=false 时 error 原样透传（401/额度不足/模型不存在等信息要能给前端看）。
    传 {id} 可直接测已存配置；显式传的 base_url/api_key/model 优先（测未保存的草稿）。
    """
    base_url, api_key, model = req.base_url, req.api_key, req.model
    if req.id is not None:
        row = await llm_provider_store.get_provider(req.id)
        if not row:
            raise HTTPException(status_code=404, detail="供应商不存在")
        base_url = base_url or row.base_url
        api_key = api_key or row.api_key
        model = model or row.chat_model
    if not (base_url and api_key and model):
        raise HTTPException(status_code=422, detail="base_url / api_key / model 不能同时为空（或传 id 测已存配置）")

    started = time.time()
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回复：OK"}],
            max_tokens=512,
        )
        content = (resp.choices[0].message.content or "").strip()
        return {
            "ok": True,
            "latency_ms": int((time.time() - started) * 1000),
            "content": content[:50],
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((time.time() - started) * 1000),
            "content": "",
            "error": str(exc),
        }


@router.post("/llm-providers/fetch-models")
async def fetch_llm_provider_models(req: LLMProviderFetchModelsRequest, authorized: AdminRequired):
    """拉取供应商的可用模型 id 列表（GET {base_url}/models）。"""
    try:
        client = AsyncOpenAI(base_url=req.base_url, api_key=req.api_key, timeout=15)
        models = [m.id async for m in client.models.list()]
        return {"models": models}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"拉取模型列表失败：{exc}")
