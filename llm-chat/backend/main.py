"""
author: leizihao
email: lzh19162600626@gmail.com
"""
import logging
import uuid
import json
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from models import ChatRequest, CreateConversationRequest, UpdateConversationRequest
from harness import harness
from layers.capability import list_models, get_embedding   # 第 2 层
from layers.extension import apply_cors                    # 第 9 层
from layers import longterm                                # 第 3b 层
from config import CHAT_MODEL, BACKEND_HOST, BACKEND_PORT, EMBEDDING_MODEL

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动 ──
    try:
        await longterm.init_collection()
    except Exception as exc:
        logger.error("Qdrant 初始化失败（长期记忆不可用）: %s", exc)
    yield
    # ── 关闭 ──（无需操作）


app = FastAPI(title="WeChat", lifespan=lifespan)
apply_cors(app)  # 第 9 层 – Extension


# ── 模型 ──

@app.get("/api/models")
async def get_models():
    models = await list_models()   # Layer 2 – Capability
    return {"models": models}


# ── 对话管理 ──

@app.get("/api/conversations")
async def get_conversations():
    return {"conversations": harness.list_conversations()}


@app.post("/api/conversations")
async def create_conversation(req: CreateConversationRequest):
    conv_id = str(uuid.uuid4())[:8]
    conv = harness.create_conversation(
        conv_id=conv_id,
        title=req.title or "新对话",
        system_prompt=req.system_prompt or "",
    )
    return {"id": conv.id, "title": conv.title}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = harness.get_conversation(conv_id)
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
    conv = harness.get_conversation(conv_id)
    if not conv:
        return {"error": "对话不存在"}
    if req.title is not None:
        conv.title = req.title
    if req.system_prompt is not None:
        conv.system_prompt = req.system_prompt
    harness._save(conv)
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    harness.delete_conversation(conv_id)
    await longterm.delete_by_conv(conv_id)   # Layer 3b – 同步清除向量记忆
    return {"ok": True}


# ── 聊天（流式 SSE） ──

@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv = harness.get_conversation(req.conversation_id)
    if not conv:
        conv = harness.create_conversation(req.conversation_id)

    harness.add_message(req.conversation_id, "user", req.message)

    # 第 3b 层 – 检索长期记忆（在 build_messages 之前）
    long_term = await harness.search_long_term(req.conversation_id, req.message)

    messages = harness.gen_chat_msg(conv, long_term)   # 第 6 层 – Context
    model = req.model or CHAT_MODEL

    async def generate():
        full_response = ""
        async for chunk in harness.chat_stream(          # 第 4 层 – Runtime
            model=model,
            messages=messages,
            temperature=req.temperature,
        ):
            full_response += chunk
            yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

        harness.add_message(req.conversation_id, "assistant", full_response)

        # 第 3b 层 – 存储本轮对话到长期记忆
        await harness.store_long_term(req.conversation_id, req.message, full_response)

        compressed = await harness.maybe_compress(req.conversation_id)  # 第 6 层
        yield f"data: {json.dumps({'done': True, 'compressed': compressed})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 记忆调试 ──

@app.get("/api/conversations/{conv_id}/memory")
async def get_memory_debug(conv_id: str):
    conv = harness.get_conversation(conv_id)
    if not conv:
        return {"error": "对话不存在"}
    lt_count = await longterm.count_by_conv(conv_id)
    return {
        "total_messages": len(conv.messages),
        "summarised_count": conv.mid_term_cursor,
        "window_count": len(conv.messages) - conv.mid_term_cursor,
        "mid_term_summary": conv.mid_term_summary or "(空)",
        "long_term_stored_pairs": lt_count,
        "build_messages_preview": [
            {
                "role": m["role"],
                "content": m["content"][:80] + "..." if len(m["content"]) > 80 else m["content"],
            }
            for m in harness.gen_chat_msg(conv)
        ],
    }


# ── Embedding 测试接口 ──

@app.post("/api/embedding")
async def test_embedding(text: str = "测试文本"):
    vec = await get_embedding(text, EMBEDDING_MODEL)  # Layer 2 – Capability
    return {
        "model": EMBEDDING_MODEL,
        "text": text,
        "dimensions": len(vec),
        "vector_preview": vec[:5],
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("harness")
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT)
