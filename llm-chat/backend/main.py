"""
ChatFlow Backend —— LangChain + LangGraph 重构版
author: leizihao
email: lzh19162600626@gmail.com

启动顺序（lifespan）：
  1. 从磁盘加载全部对话到内存
  2. 初始化 Qdrant Collection（若启用长期记忆）
  3. 加载 MCP 工具（若配置了 MCP_SERVERS）
  4. 构建并编译 LangGraph Agent 图

REST API 与原版完全兼容，前端无需修改。
新增接口：
  GET  /api/tools          —— 查看当前可用工具列表
  GET  /api/conversations/{id}/memory  —— 记忆状态调试（新增 active_tools 字段）
"""
import logging
import uuid

import httpx
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from models import ChatRequest, CreateConversationRequest, UpdateConversationRequest
from memory import store as memory_store
from rag import retriever as rag_retriever
from tools.mcp.loader import load_mcp_tools
from tools import get_all_tools, get_tool_names, get_tools_info
from graph import agent as graph_agent
from graph import runner as graph_runner
from layers.extension import apply_cors  # 保留原有 CORS 配置
from config import (
    CHAT_MODEL,
    BACKEND_HOST,
    BACKEND_PORT,
    EMBEDDING_MODEL,
    LONGTERM_MEMORY_ENABLED,
    MCP_SERVERS, API_BASE_URL,
)

logger = logging.getLogger("main")


# ── 应用生命周期 ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动 ──

    # 1. 从磁盘加载对话历史
    memory_store.init()

    # 2. 初始化 Qdrant
    if LONGTERM_MEMORY_ENABLED:
        try:
            await rag_retriever.init_collection()
        except Exception as exc:
            logger.error("Qdrant 初始化失败（长期记忆不可用）: %s", exc)
    else:
        logger.info("长期记忆（RAG）已禁用，跳过 Qdrant 初始化")

    # 3. 加载 MCP 工具
    if MCP_SERVERS:
        await load_mcp_tools(MCP_SERVERS)
    else:
        logger.info("未配置 MCP 服务器，跳过 MCP 工具加载")

    # 4. 构建 LangGraph Agent 图
    all_tools = get_all_tools()
    graph_agent.init(tools=all_tools, model=CHAT_MODEL)

    logger.info(
        "ChatFlow 启动完成 | 模型: %s | 工具数: %d | 长期记忆: %s",
        CHAT_MODEL,
        len(all_tools),
        "开启" if LONGTERM_MEMORY_ENABLED else "关闭",
    )

    yield
    # ── 关闭（无需操作，Qdrant 客户端无持久连接） ──


app = FastAPI(title="ChatFlow", version="2.0.0", lifespan=lifespan)
apply_cors(app)


# ── 模型接口 ──────────────────────────────────────────────────────────────────

@app.get("/api/models")
async def get_models():
    """列出 Ollama 中已下载的所有模型。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{API_BASE_URL}/api/tags")
            data = resp.json()
            models = [m["name"] for m in data.get("models", []) if not m["name"].startswith(EMBEDDING_MODEL.split(":")[0])]
    except Exception:
        models = [CHAT_MODEL]
    return {"models": models}


# ── 对话管理 ──────────────────────────────────────────────────────────────────

@app.get("/api/conversations")
async def get_conversations():
    convs = sorted(
        [
            {"id": c.id, "title": c.title, "updated_at": c.updated_at}
            for c in memory_store.all_conversations()
        ],
        key=lambda x: x["updated_at"],
        reverse=True,
    )
    return {"conversations": convs}


@app.post("/api/conversations")
async def create_conversation(req: CreateConversationRequest):
    conv_id = str(uuid.uuid4())[:8]
    conv = memory_store.create(
        conv_id=conv_id,
        title=req.title or "新对话",
        system_prompt=req.system_prompt or "",
    )
    return {"id": conv.id, "title": conv.title}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = memory_store.get(conv_id)
    if not conv:
        return {"error": "对话不存在"}
    return {
        "id": conv.id,
        "title": conv.title,
        "system_prompt": conv.system_prompt,
        "messages": [
            {"role": m.role, "content": m.content, "timestamp": m.timestamp}
            for m in conv.messages
        ],
        "mid_term_summary": conv.mid_term_summary,
    }


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: UpdateConversationRequest):
    conv = memory_store.get(conv_id)
    if not conv:
        return {"error": "对话不存在"}
    if req.title is not None:
        conv.title = req.title
    if req.system_prompt is not None:
        conv.system_prompt = req.system_prompt
    memory_store.save(conv)
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    memory_store.delete(conv_id)
    if LONGTERM_MEMORY_ENABLED:
        await rag_retriever.delete_by_conv(conv_id)
    return {"ok": True}


# ── 聊天（流式 SSE） ──────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """
    流式对话接口（SSE）。

    SSE 事件格式：
      data: {"content": "..."}         ← 增量 token
      data: {"tool_call": {...}}        ← 工具调用
      data: {"search_item": {...}}      ← 单条搜索结果（实时追加）
      data: {"tool_result": {...}}      ← 工具完成信号
      data: {"done": true, "compressed": bool}  ← 完成信号
    """
    conv = memory_store.get(req.conversation_id)
    if not conv:
        conv = memory_store.create(req.conversation_id)

    async def safe_stream():
        async for chunk in graph_runner.stream_response(
            conv_id=req.conversation_id,
            user_message=req.message,
            model=req.model or CHAT_MODEL,
            temperature=req.temperature,
        ):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        safe_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 工具接口 ──────────────────────────────────────────────────────────────────

@app.get("/api/tools")
async def list_tools():
    """列出当前所有可用工具（内置 + MCP + 动态注册）。"""
    return {"tools": get_tools_info()}


# ── 记忆调试接口 ───────────────────────────────────────────────────────────────

@app.get("/api/conversations/{conv_id}/memory")
async def get_memory_debug(conv_id: str):
    conv = memory_store.get(conv_id)
    if not conv:
        return {"error": "对话不存在"}
    lt_count = (
        await rag_retriever.count_by_conv(conv_id) if LONGTERM_MEMORY_ENABLED else -1
    )
    return {
        "total_messages": len(conv.messages),
        "summarised_count": conv.mid_term_cursor,
        "window_count": len(conv.messages) - conv.mid_term_cursor,
        "mid_term_summary": conv.mid_term_summary or "(空)",
        "long_term_stored_pairs": lt_count if LONGTERM_MEMORY_ENABLED else "(已禁用)",
        "active_tools": get_tool_names(),
    }


# ── Embedding 测试接口 ────────────────────────────────────────────────────────

@app.post("/api/embedding")
async def test_embedding(text: str = "测试文本"):
    from llm.embeddings import embed_text
    vec = await embed_text(text)
    return {
        "model": EMBEDDING_MODEL,
        "text": text,
        "dimensions": len(vec),
        "vector_preview": vec[:5],
    }


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("启动程序")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT)
