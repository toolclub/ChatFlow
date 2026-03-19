import uuid
import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from models import ChatRequest, CreateConversationRequest, UpdateConversationRequest
from harness import harness
from layers.capability import list_models, get_embedding   # Layer 2
from layers.extension import apply_cors                    # Layer 9
from config import CHAT_MODEL, BACKEND_HOST, BACKEND_PORT, EMBEDDING_MODEL

app = FastAPI(title="本地LLM对话服务")
apply_cors(app)  # Layer 9 – Extension


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
    return {"ok": True}


# ── 聊天（流式 SSE） ──

@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv = harness.get_conversation(req.conversation_id)
    if not conv:
        conv = harness.create_conversation(req.conversation_id)

    harness.add_message(req.conversation_id, "user", req.message)
    messages = harness.build_messages(conv)       # Layer 6 – Context
    model = req.model or CHAT_MODEL

    async def generate():
        full_response = ""
        async for chunk in harness.chat_stream(   # Layer 4 – Runtime
            model=model,
            messages=messages,
            temperature=req.temperature,
        ):
            full_response += chunk
            yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

        harness.add_message(req.conversation_id, "assistant", full_response)
        compressed = await harness.maybe_compress(req.conversation_id)  # Layer 6
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
    return {
        "total_messages": len(conv.messages),
        "summarised_count": conv.mid_term_cursor,
        "window_count": len(conv.messages) - conv.mid_term_cursor,
        "mid_term_summary": conv.mid_term_summary or "(空)",
        "build_messages_preview": [
            {
                "role": m["role"],
                "content": m["content"][:80] + "..." if len(m["content"]) > 80 else m["content"],
            }
            for m in harness.build_messages(conv)
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
    import uvicorn
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT)
