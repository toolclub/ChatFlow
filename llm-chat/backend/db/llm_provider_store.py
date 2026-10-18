"""
LLM 供应商配置存取层 — 纯 DB CRUD，不持有进程内状态

is_active「至多一条 True」由 activate_provider 在单事务内保证：
先 UPDATE 全部置 False，再把目标行置 True，中途失败整体回滚。
进程内快照与跨 worker 失效见 services/llm_provider_service.py。
"""
import logging
import time

from sqlalchemy import select, update

from db.database import AsyncSessionLocal
from db.models import LLMProviderModel

logger = logging.getLogger("db.llm_provider_store")


async def list_providers() -> list[LLMProviderModel]:
    """全部配置，按 id 稳定排序（管理页列表顺序可预期）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LLMProviderModel).order_by(LLMProviderModel.id)
        )
        return list(result.scalars().all())


async def get_provider(pid: int) -> LLMProviderModel | None:
    async with AsyncSessionLocal() as session:
        return await session.get(LLMProviderModel, pid)


async def get_provider_by_name(name: str) -> LLMProviderModel | None:
    """按显示名查重（create/update 前置校验，返回 409 用）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LLMProviderModel).where(LLMProviderModel.name == name).limit(1)
        )
        return result.scalars().first()


async def create_provider(**fields) -> LLMProviderModel:
    """新建配置。name 撞唯一索引会抛 IntegrityError，由路由层转 409。"""
    now = time.time()
    async with AsyncSessionLocal() as session:
        row = LLMProviderModel(created_at=now, updated_at=now, **fields)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def update_provider(pid: int, **fields) -> LLMProviderModel | None:
    """按字段部分更新；目标不存在返回 None（路由层转 404）。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(LLMProviderModel, pid)
        if not row:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = time.time()
        await session.commit()
        await session.refresh(row)
        return row


async def delete_provider(pid: int) -> bool:
    """删除配置；不存在返回 False。删的是激活项时由路由层负责失效广播。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(LLMProviderModel, pid)
        if not row:
            return False
        was_active = bool(row.is_active)
        await session.delete(row)
        await session.commit()
        if was_active:
            logger.info("删除的 LLM 供应商是激活项，运行时将回退 .env 默认 | id=%s", pid)
        return True


async def get_active_provider() -> LLMProviderModel | None:
    """当前生效配置（is_active=True 且 enabled=True）；无则回退 .env。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LLMProviderModel)
            .where(LLMProviderModel.is_active.is_(True), LLMProviderModel.enabled.is_(True))
            .limit(1)
        )
        return result.scalars().first()


async def activate_provider(pid: int) -> LLMProviderModel | None:
    """激活指定配置（单事务：先清空所有 is_active，再置目标 True）。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(LLMProviderModel, pid)
        if not row:
            return None
        await session.execute(
            update(LLMProviderModel)
            .where(LLMProviderModel.is_active.is_(True))
            .values(is_active=False)
        )
        row.is_active = True
        row.updated_at = time.time()
        await session.commit()
        await session.refresh(row)
        return row


async def deactivate_all() -> int:
    """全部取消激活（「恢复 .env 默认」）。返回受影响行数。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(LLMProviderModel)
            .where(LLMProviderModel.is_active.is_(True))
            .values(is_active=False)
        )
        await session.commit()
        return result.rowcount or 0
