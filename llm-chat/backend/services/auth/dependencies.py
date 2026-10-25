from fastapi import Depends, Request, HTTPException, Header
from typing import Annotated
import secrets
from .jwt_handler import verify_token
from db.user_store import get_user_by_id

import config

async def get_current_user(request: Request) -> dict:
    """
    认证依赖注入，返回用户信息。
    
    优先级：
    1. Authorization: Bearer <jwt> → 验证 JWT
    2. X-Client-ID → 兼容匿名用户（过渡期）
    
    返回：
    - 已登录: {"id": "uuid", "name": "...", "is_anonymous": False}
    - 匿名: {"id": None, "client_id": "...", "is_anonymous": True}
    """
    client_id = request.headers.get("X-Client-ID", "").strip()
    if len(client_id) > 36:
        raise HTTPException(status_code=400, detail="X-Client-ID 长度不能超过 36 个字符")
    # A supplied but expired token must remain an authentication failure. Falling
    # back to guest identity hides signed-in history and prevents client refresh.
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        scheme, _, token = auth_header.partition(" ")
        payload = verify_token(token) if scheme.lower() == "bearer" and token else None
        if not payload or payload.get("type") != "access" or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="登录已过期，请刷新登录状态")
        user = await get_user_by_id(payload["sub"])
        if not user or not user.get("is_active"):
            raise HTTPException(status_code=401, detail="登录用户不存在或已停用")
        return {**user, "client_id": client_id, "is_anonymous": False}

    client_id = request.headers.get("X-Client-ID", "").strip()
    if not client_id:
        raise HTTPException(status_code=401, detail="缺少用户身份")
    if len(client_id) > 36:
        raise HTTPException(status_code=400, detail="X-Client-ID 长度不能超过 36 个字符")
    return {"id": None, "client_id": client_id, "is_anonymous": True}

async def require_user(
    user: Annotated[dict, Depends(get_current_user)]
) -> dict:
    """强制登录要求"""
    if user["is_anonymous"]:
        raise HTTPException(status_code=401, detail="此操作需要登录")
    return user

# 类型别名
CurrentUser = Annotated[dict, Depends(get_current_user)]
RequiredUser = Annotated[dict, Depends(require_user)]


# ── 管理员密钥鉴权（复用现有机制，供 quant + prediction 共用） ────────────────
# 密钥源 config.ADMIN_SECRET_KEY（.env 中 ENC(...) 加密，import 时 decrypt_env 解密）——不硬编码。
# 铁律 #1：拦截来自后端 403，非前端 v-show。
async def verify_admin_access(x_admin_key: str = Header(None)) -> bool:
    """校验 X-Admin-Key header == ADMIN_SECRET_KEY，失败 403。"""
    if not config.ADMIN_SECRET_KEY or not x_admin_key or not secrets.compare_digest(x_admin_key, config.ADMIN_SECRET_KEY):
        raise HTTPException(status_code=403, detail="管理员权限验证失败")
    return True


AdminRequired = Annotated[bool, Depends(verify_admin_access)]
