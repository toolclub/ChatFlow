"""
SQLAlchemy 异步数据库连接配置
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine_from_url(database_url: str):
    """将 postgresql:// 转换为 postgresql+asyncpg:// 并创建异步引擎"""
    url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )


# 延迟初始化，避免 import 时立即连接
_engine = None
_AsyncSessionLocal = None


def init_engine(database_url: str) -> None:
    global _engine, _AsyncSessionLocal
    _engine = create_engine_from_url(database_url)
    _AsyncSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


def get_engine():
    return _engine


def AsyncSessionLocal():
    """返回 async session 上下文管理器（兼容 async with AsyncSessionLocal() as session:）"""
    if _AsyncSessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_engine() first.")
    return _AsyncSessionLocal()
