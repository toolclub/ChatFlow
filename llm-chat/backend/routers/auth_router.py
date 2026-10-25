import time
import secrets
import logging
import traceback
from typing import Optional, Literal
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from config import settings, COOKIE_SECURE
from services.auth.jwt_handler import create_access_token, create_refresh_token, verify_token
from services.auth.dependencies import CurrentUser, RequiredUser
from services.auth.oauth_google import GoogleOAuth
from services.auth.oauth_github import GitHubOAuth
from db import user_store, session_store
from db.redis_state import _get_redis

logger = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── OAuth State 管理（Redis 存储，防 CSRF）───────────────────────────────────
_STATE_KEY_PREFIX = "chatflow:oauth_state:"
_STATE_TTL = 300  # 5 分钟过期

async def _store_oauth_state(state: str, client_id: str) -> None:
    """存储 OAuth state 到 Redis"""
    r = _get_redis()
    await r.set(f"{_STATE_KEY_PREFIX}{state}", client_id, ex=_STATE_TTL)

async def _verify_oauth_state(state: str) -> Optional[str]:
    """验证 OAuth state，返回关联的 client_id，验证后自动删除"""
    r = _get_redis()
    key = f"{_STATE_KEY_PREFIX}{state}"
    return await r.getdel(key)  # atomic single-use, including an empty guest ID

# OAuth Provider 映射
PROVIDERS = {
    "google": GoogleOAuth(),
    "github": GitHubOAuth(),
}

@router.get("/oauth/{provider}/login")
async def oauth_login(provider: str, client_id: str = ""):
    """发起 OAuth 登录"""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported provider")

    if len(client_id) > 36:
        raise HTTPException(status_code=400, detail="client_id 长度不能超过 36 个字符")

    # 生成随机 state token（仅包含随机部分，不含 client_id）
    state_token = secrets.token_urlsafe(32)

    # 存储 state → client_id 映射到 Redis
    await _store_oauth_state(state_token, client_id)

    # 获取跳转 URL（state 只传随机 token）
    login_url = PROVIDERS[provider].get_login_url(state_token)
    return RedirectResponse(login_url)

@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
):
    """OAuth 回调"""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported provider")

    # 验证 state 并获取关联的 client_id
    client_id = await _verify_oauth_state(state)
    if client_id is None:
        logger.warning("OAuth state 验证失败或已过期 | state=%s", state[:16])
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    try:
        # 1. 换取用户信息
        oauth_user = await PROVIDERS[provider].exchange_code_for_user(code)

        # 2. 查找或创建用户
        user = await user_store.get_user_by_oauth(provider, oauth_user["provider_id"])
        if not user:
            # 尝试按邮箱查找
            user = await user_store.get_user_by_email(oauth_user["email"])
            if user:
                # 存在相同邮箱用户，自动关联 OAuth
                pass  # 后续可以添加 link 逻辑
            else:
                # 创建新用户
                user = await user_store.create_user_with_oauth(
                    user_data={
                        "email": oauth_user["email"],
                        "name": oauth_user["name"],
                        "avatar_url": oauth_user["avatar_url"]
                    },
                    oauth_data=oauth_user
                )

        # 3. 关联 client_id 数据（首次登录迁移匿名数据）
        if client_id:
            try:
                await user_store.migrate_client_data(client_id, user["id"])
                logger.info("数据迁移成功 | client=%s → user=%s", client_id[:8], user["id"][:8])
            except Exception as e:
                logger.error("数据迁移失败: %s", e)
                # 迁移失败不应阻断登录

        # 4. 更新登录时间
        await user_store.update_last_login(user["id"])

        # 5. 生成 Token
        access_token = create_access_token(user["id"])
        refresh_token = create_refresh_token(user["id"])

        # 6. 存储 Session
        await session_store.create_session(user["id"], refresh_token)

        # 8. 重定向回前端 (使用 Fragment # 传递 access_token)
        redirect_url = f"{settings.frontend_url}/#auth_success=1&access_token={access_token}"
        resp = RedirectResponse(redirect_url)

        # 7. 设置 HttpOnly Cookie（安全配置从环境变量读取）
        # 同域名使用 samesite="lax"，显式设置 domain 使 Cookie 在 www/non-www 子域共享
        # 先清理旧格式 Cookie（无 domain，过渡期兼容），防止同名 Cookie 冲突
        resp.delete_cookie(
            key="refresh_token",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        resp.set_cookie(
            key="refresh_token",
            value=refresh_token,
            max_age=settings.jwt_refresh_expire_days * 24 * 60 * 60,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
            domain=settings.cookie_domain,
        )

        return resp

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OAuth callback 失败 | type=%s | msg=%s | traceback:\n%s",
                     type(e).__name__, str(e), traceback.format_exc())
        detail = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__}"
        raise HTTPException(status_code=500, detail=detail)

@router.post("/token/refresh")
async def refresh_token(request: Request, response: Response):
    """刷新 Access Token"""
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    payload = verify_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    user = await user_store.get_user_by_id(user_id) if user_id else None
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="User unavailable")
    # 验证 Session 是否有效
    session = await session_store.validate_refresh_token(user_id, refresh_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired or deactivated")

    # 生成新 Access Token
    new_access_token = create_access_token(user_id)
    return {
        "access_token": new_access_token,
        "expires_in": settings.jwt_access_expire_minutes * 60
    }

@router.get("/me")
async def get_me(user: RequiredUser):
    """获取当前用户信息"""
    settings_data = await user_store.get_user_settings(user["id"])
    return {
        "user": user,
        "settings": settings_data,
        "oauth_accounts": []  # TODO: 返回关联的账号列表
    }

class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=100)
    avatar_url: str | None = Field(None, max_length=512)
    bio: str | None = Field(None, max_length=20000)
    locale: str | None = Field(None, max_length=10)
    timezone: str | None = Field(None, max_length=50)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["system", "light", "dark"] | None = None
    default_model: str | None = Field(None, max_length=100)
    agent_mode_default: bool | None = None
    language: str | None = Field(None, max_length=10)
    notifications_enabled: bool | None = None
    sidebar_collapsed: bool | None = None
    custom_settings: dict | None = None


@router.put("/me")
async def update_me(data: ProfileUpdate, user: RequiredUser):
    """Persist profile edits and return the saved profile."""
    return await user_store.update_user_profile(user["id"], data.model_dump(exclude_none=True))


@router.get("/me/settings")
async def get_settings(user: RequiredUser):
    return await user_store.get_user_settings(user["id"])


@router.put("/me/settings")
async def update_settings(data: SettingsUpdate, user: RequiredUser):
    await user_store.update_user_settings(user["id"], data.model_dump(exclude_none=True))
    return await user_store.get_user_settings(user["id"])

@router.get("/me/sessions")
async def get_sessions(user: RequiredUser):
    """获取活跃会话"""
    sessions = await session_store.list_active_sessions(user["id"])
    return {"sessions": sessions}

@router.post("/logout")
async def logout(request: Request, response: Response, user: RequiredUser):
    """退出登录"""
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        await session_store.deactivate_session_by_token(user["id"], refresh_token)

    response.delete_cookie(
        key="refresh_token",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain,
    )
    return {"success": True}

@router.post("/logout/all")
async def logout_all(response: Response, user: RequiredUser):
    """退出所有设备"""
    await session_store.deactivate_all_sessions(user["id"])
    response.delete_cookie(
        key="refresh_token",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain,
    )
    return {"success": True}