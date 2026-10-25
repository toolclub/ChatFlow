import time
import hashlib
import uuid
from typing import Optional, List
from sqlalchemy import select, update, delete
from db.database import AsyncSessionLocal
from config import settings
from db.models import SessionModel

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

async def create_session(user_id: str, refresh_token: str, device_info: str = "", ip_address: str = ""):
    async with AsyncSessionLocal() as session:
        now = time.time()
        new_session = SessionModel(
            id=str(uuid.uuid4()),
            user_id=user_id,
            refresh_token_hash=_hash_token(refresh_token),
            device_info=device_info,
            ip_address=ip_address,
            is_active=True,
            expires_at=now + settings.jwt_refresh_expire_days * 24 * 60 * 60,
            created_at=now
        )
        session.add(new_session)
        await session.commit()
        return new_session.id

async def validate_refresh_token(user_id: str, refresh_token: str) -> Optional[SessionModel]:
    async with AsyncSessionLocal() as session:
        token_hash = _hash_token(refresh_token)
        stmt = select(SessionModel).where(
            SessionModel.user_id == user_id,
            SessionModel.refresh_token_hash == token_hash,
            SessionModel.is_active == True,
            SessionModel.expires_at > time.time()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

async def deactivate_session_by_token(user_id: str, refresh_token: str):
    async with AsyncSessionLocal() as session:
        token_hash = _hash_token(refresh_token)
        await session.execute(
            update(SessionModel).where(
                SessionModel.user_id == user_id,
                SessionModel.refresh_token_hash == token_hash
            ).values(is_active=False)
        )
        await session.commit()

async def deactivate_all_sessions(user_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SessionModel).where(SessionModel.user_id == user_id).values(is_active=False)
        )
        await session.commit()

async def list_active_sessions(user_id: str):
    async with AsyncSessionLocal() as session:
        stmt = select(SessionModel).where(
            SessionModel.user_id == user_id,
            SessionModel.is_active == True,
            SessionModel.expires_at > time.time()
        )
        result = await session.execute(stmt)
        sessions = result.scalars().all()
        return [{
            "id": s.id,
            "device_info": s.device_info,
            "ip_address": s.ip_address,
            "created_at": s.created_at,
            "expires_at": s.expires_at
        } for s in sessions]
