import time
import uuid
from typing import Optional, List
from sqlalchemy import select, update, insert
from db.database import AsyncSessionLocal
from db.models import UserModel, UserSettingsModel, OAuthAccountModel, ConversationModel, QuantSnapshotModel

async def migrate_client_data(client_id: str, user_id: str):
    """将匿名数据迁移到登录用户下"""
    async with AsyncSessionLocal() as session:
        # 1. 迁移对话
        await session.execute(
            update(ConversationModel)
            .where(ConversationModel.client_id == client_id, ConversationModel.user_id == "")
            .values(user_id=user_id)
        )
        # 2. 迁移量化快照
        await session.execute(
            update(QuantSnapshotModel)
            .where(QuantSnapshotModel.client_id == client_id, QuantSnapshotModel.user_id == "")
            .values(user_id=user_id)
        )
        await session.commit()


async def get_user_by_id(user_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserModel).where(UserModel.id == user_id))
        user = result.scalar_one_or_none()
        return _to_dict(user) if user else None

async def get_user_by_email(email: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserModel).where(UserModel.email == email))
        user = result.scalar_one_or_none()
        return _to_dict(user) if user else None

async def get_user_by_oauth(provider: str, provider_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        stmt = select(UserModel).join(OAuthAccountModel).where(
            OAuthAccountModel.provider == provider,
            OAuthAccountModel.provider_id == provider_id
        )
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        return _to_dict(user) if user else None

async def create_user_with_oauth(user_data: dict, oauth_data: dict) -> dict:
    async with AsyncSessionLocal() as session:
        now = time.time()
        user_id = str(uuid.uuid4())
        
        # 1. 创建用户
        user = UserModel(
            id=user_id,
            email=user_data["email"],
            name=user_data["name"],
            avatar_url=user_data.get("avatar_url", ""),
            is_active=True,
            created_at=now,
            updated_at=now
        )
        session.add(user)
        
        # 2. 创建 OAuth 关联
        oauth = OAuthAccountModel(
            user_id=user_id,
            provider=oauth_data["provider"],
            provider_id=oauth_data["provider_id"],
            provider_email=oauth_data.get("email", ""),
            provider_name=oauth_data.get("name", ""),
            provider_avatar=oauth_data.get("avatar_url", ""),
            raw_profile=oauth_data.get("raw_profile", {}),
            created_at=now,
            updated_at=now
        )
        session.add(oauth)
        
        # 3. 创建默认设置
        settings = UserSettingsModel(
            user_id=user_id,
            created_at=now,
            updated_at=now
        )
        session.add(settings)
        
        await session.commit()
        return _to_dict(user)

async def update_last_login(user_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(last_login_at=time.time())
        )
        await session.commit()

async def get_user_settings(user_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserSettingsModel).where(UserSettingsModel.user_id == user_id))
        settings = result.scalar_one_or_none()
        if not settings:
            return None
        return {
            "theme": settings.theme,
            "default_model": settings.default_model,
            "agent_mode_default": settings.agent_mode_default,
            "language": settings.language,
            "notifications_enabled": settings.notifications_enabled,
            "sidebar_collapsed": settings.sidebar_collapsed,
            "custom_settings": settings.custom_settings,
        }

async def update_user_profile(user_id: str, data: dict) -> Optional[dict]:
    allowed = {"name", "avatar_url", "bio", "locale", "timezone"}
    values = {key: value for key, value in data.items() if key in allowed and value is not None}
    async with AsyncSessionLocal() as session:
        if values:
            await session.execute(update(UserModel).where(UserModel.id == user_id).values(**values, updated_at=time.time()))
            await session.commit()
    return await get_user_by_id(user_id)


async def update_user_settings(user_id: str, data: dict):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    allowed = {"theme", "default_model", "agent_mode_default", "language", "notifications_enabled", "sidebar_collapsed", "custom_settings"}
    values = {key: value for key, value in data.items() if key in allowed and value is not None}
    now = time.time()
    async with AsyncSessionLocal() as session:
        statement = pg_insert(UserSettingsModel).values(user_id=user_id, created_at=now, updated_at=now, **values)
        statement = statement.on_conflict_do_update(index_elements=[UserSettingsModel.user_id], set_={**values, "updated_at": now})
        await session.execute(statement)
        await session.commit()

def _to_dict(user: UserModel) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "locale": user.locale,
        "timezone": user.timezone,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
    }
