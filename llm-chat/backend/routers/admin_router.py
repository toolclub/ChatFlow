"""
管理者页面路由 — 系统运行状况监控与 Token 统计

职责：
  - 验证管理者密钥
  - 聚合用户访问数据
  - 统计 Token 消耗情况 (Prompt/Completion/Reasoning)
  - 提供 ECharts 所需的结构化数据
"""
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException, Header, Depends
from sqlalchemy import select, func, desc
from pydantic import BaseModel

from db.database import AsyncSessionLocal
from db.models import UserModel, ConversationModel, MessageModel, UserUsageLogModel
from services.auth.dependencies import CurrentUser, verify_admin_access
import config

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
