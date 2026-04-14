"""
ChatFlow Backend —— 应用入口（仅负责启动和路由注册）

类似 Spring Boot 的 Application 类：
  - lifespan: 初始化基础设施（DB / Cache / RAG / MCP / Sandbox / Graph）
  - 路由注册: 将各 router 模块挂载到 FastAPI app
  - 入口: uvicorn 启动

业务逻辑全部在 routers/ + services/ 层。

author: leizihao
email: lzh19162600626@gmail.com
"""
import logging

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from layers.extension import apply_cors
from db.database import init_engine, get_engine
from db.models import Base
from logging_config import setup_logging
from cache.factory import init_cache
from config import (
    CHAT_MODEL,
    BACKEND_HOST,
    BACKEND_PORT,
    LONGTERM_MEMORY_ENABLED,
    MCP_SERVERS,
    SEMANTIC_CACHE_ENABLED,
    DATABASE_URL,
    LOG_DIR,
)

logger = logging.getLogger("main")


# ── 应用生命周期 ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动 ──

    # 0. 初始化日志系统
    setup_logging(LOG_DIR)

    # 1. 初始化数据库连接并自动建表 + 增量迁移
    init_engine(DATABASE_URL)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from db.migrate import run_migrations
        await run_migrations(conn)
    logger.info("数据库初始化完成")

    # 2. 从数据库加载对话到内存缓存
    from memory import store as memory_store
    await memory_store.init()

    # 3. 初始化语义缓存（Redis Search）
    try:
        await init_cache()
        try:
            from cache.factory import get_cache
            await get_cache().clear()
            logger.info("语义缓存已清理（启动时一次性清除旧数据）")
        except Exception:
            pass
    except Exception as exc:
        logger.error("语义缓存初始化失败（已降级为 NullCache）: %s", exc)

    # 4. 初始化 Qdrant
    if LONGTERM_MEMORY_ENABLED:
        try:
            from rag import retriever as rag_retriever
            await rag_retriever.init_collection()
        except Exception as exc:
            logger.error("Qdrant 初始化失败（长期记忆不可用）: %s", exc)
    else:
        logger.info("长期记忆（RAG）已禁用，跳过 Qdrant 初始化")

    # ── 工具注册（全部由 SkillRegistry 统一管理，目录即分类） ────────────────
    from tools import discover, get_all_tools

    # 5. 注册内置工具（tools/builtin/ 目录，不依赖外部服务）
    discover("tools.builtin")

    # 5.5 加载 MCP 工具
    if MCP_SERVERS:
        from tools.mcp.loader import load_mcp_tools
        await load_mcp_tools(MCP_SERVERS)
    else:
        logger.info("未配置 MCP 服务器，跳过 MCP 工具加载")

    # 6. 初始化沙箱 → 注册沙箱工具（tools/sandbox/ 目录）
    sandbox_ok = False
    from config import SANDBOX_ENABLED, SANDBOX_WORKERS, SANDBOX_TIMEOUT, SANDBOX_CLEANUP_HOURS
    if SANDBOX_ENABLED and SANDBOX_WORKERS:
        from sandbox.manager import sandbox_manager
        try:
            await sandbox_manager.init(SANDBOX_WORKERS, SANDBOX_TIMEOUT, SANDBOX_CLEANUP_HOURS)
            if sandbox_manager.available:
                sandbox_ok = discover("tools.sandboxed") > 0
            else:
                logger.warning("沙箱 Worker 全部连接失败，沙箱工具未注册（模型不可见）")
        except Exception as exc:
            logger.error("沙箱初始化异常，沙箱工具未注册: %s", exc)
    else:
        logger.info("沙箱代码执行已禁用（SANDBOX_ENABLED=false 或无 Worker 配置）")

    # 7. 构建 LangGraph Agent 图（用注册表中的全部工具）
    from graph import agent as graph_agent
    all_tools = get_all_tools()
    graph_agent.init(tools=all_tools, model=CHAT_MODEL)

    logger.info(
        "ChatFlow 启动完成 | 模型: %s | 工具数: %d | 长期记忆: %s | 语义缓存: %s | 沙箱: %s",
        CHAT_MODEL,
        len(all_tools),
        "开启" if LONGTERM_MEMORY_ENABLED else "关闭",
        "开启" if SEMANTIC_CACHE_ENABLED else "关闭",
        "开启" if sandbox_ok else "关闭",
    )

    yield
    # ── 关闭 ──
    if sandbox_ok:
        from sandbox.manager import sandbox_manager
        await sandbox_manager.shutdown()


# ── 创建应用 ──────────────────────────────────────────────────────────────────

app = FastAPI(title="ChatFlow", version="2.0.0", lifespan=lifespan)
apply_cors(app)

# ── 注册路由 ──────────────────────────────────────────────────────────────────

from routers.conversation_router import router as conversation_router
from routers.chat_router import router as chat_router
from routers.tool_router import router as tool_router
from routers.model_router import router as model_router

app.include_router(conversation_router)
app.include_router(chat_router)
app.include_router(tool_router)
app.include_router(model_router)

# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logging(LOG_DIR)
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT)
