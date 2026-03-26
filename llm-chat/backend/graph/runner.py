"""
图运行器：将 LangGraph astream_events 翻译为 FastAPI SSE 字符串流

SSE 事件格式：
  data: {"status": "routing"}\n\n               ← 路由模型判断意图中
  data: {"route": {"model": "...", "intent":"..."}} ← 路由结果（选用的模型）
  data: {"status": "thinking", "model": "..."}\n\n ← LLM 开始推理
  data: {"content": "...token..."}\n\n           ← LLM 输出 token（增量）
  data: {"tool_call": {...}}\n\n                 ← 工具调用开始
  data: {"tool_result": {...}}\n\n               ← 工具调用结果
  data: {"done": true, "compressed": bool}\n\n  ← 完成信号

关键过滤：只转发来自 "call_model" 节点的 LLM token，
忽略 compress_memory 节点内摘要模型产生的 token。
"""
import asyncio
import json
import logging
from typing import AsyncGenerator

from config import ROUTE_MODEL_MAP
from graph.agent import get_graph
from graph.state import GraphState

logger = logging.getLogger("graph.runner")

# 反查 model → intent 标签
_MODEL_TO_INTENT = {v: k for k, v in ROUTE_MODEL_MAP.items()}


async def stream_response(
    conv_id: str,
    user_message: str,
    model: str,
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    graph = get_graph(model)

    initial_state: GraphState = {
        "conv_id": conv_id,
        "user_message": user_message,
        "model": model,
        "temperature": temperature,
        "messages": [],
        "long_term_memories": [],
        "forget_mode": False,
        "full_response": "",
        "compressed": False,
        "route": "",
    }

    compressed = False
    active_model = model  # 路由后会被更新

    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            event_type: str = event["event"]
            node_name: str = event.get("metadata", {}).get("langgraph_node", "")
            event_name: str = event.get("name", "")

            # ── 路由开始 ──────────────────────────────────────────────────────
            if (event_type == "on_chain_start"
                    and (event_name == "route_model" or node_name == "route_model")):
                yield f"data: {json.dumps({'status': 'routing'})}\n\n"

            # ── 路由结束：告诉前端选了哪个模型 ──────────────────────────────
            elif (event_type == "on_chain_end"
                    and (event_name == "route_model" or node_name == "route_model")):
                output = event["data"].get("output", {})
                if isinstance(output, dict):
                    if "answer_model" in output:
                        active_model = output["answer_model"]  # ✅ 用最终回答模型
                    elif "model" in output:
                        active_model = output["model"]  # 兼容旧逻辑
                    intent = _MODEL_TO_INTENT.get(active_model, "chat")
                    payload = json.dumps(
                        {"route": {"model": active_model, "intent": intent}},
                        ensure_ascii=False,
                    )
                    yield f"data: {payload}\n\n"

            # ── LLM 开始推理 ──────────────────────────────────────────────────
            elif event_type == "on_chat_model_start" and node_name in ["call_model", "call_model_after_tool"]:
                payload = json.dumps(
                    {"status": "thinking", "model": active_model},
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n"

            # ── LLM token 流 ──────────────────────────────────────────────────
            elif event_type == "on_chat_model_stream" and node_name in ["call_model", "call_model_after_tool"]:
                chunk = event["data"].get("chunk")
                if chunk and chunk.content:
                    payload = json.dumps({"content": chunk.content}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"

            # ── 工具调用开始 ──────────────────────────────────────────────────
            elif event_type == "on_tool_start":
                tool_input = event["data"].get("input", {})
                payload = json.dumps(
                    {"tool_call": {"name": event.get("name", ""), "input": tool_input}},
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n"

            # ── 工具调用结束 ──────────────────────────────────────────────────
            elif event_type == "on_tool_end":
                name = event.get("name", "")
                raw = str(event["data"].get("output", ""))
                if name == "web_search":
                    # 逐条发送搜索结果，让前端实时追加每个 URL
                    try:
                        results = json.loads(raw)
                        for item in results:
                            url = item.get("url", "")
                            status = "done" if url else "fail"
                            si = json.dumps(
                                {"search_item": {
                                    "url": url,
                                    "title": item.get("title", ""),
                                    "status": status,
                                }},
                                ensure_ascii=False,
                            )
                            yield f"data: {si}\n\n"
                    except Exception:
                        pass
                    # 工具完成信号（不含内容，前端已通过 search_item 获取 URL）
                    payload = json.dumps({"tool_result": {"name": name}}, ensure_ascii=False)
                elif name == "fetch_webpage":
                    # 判断成功/失败（完整内容已给 LLM，前端只需状态）
                    fail_prefixes = ("读取超时", "HTTP 错误", "读取失败")
                    status = "fail" if any(raw.startswith(p) for p in fail_prefixes) else "done"
                    payload = json.dumps(
                        {"tool_result": {"name": name, "status": status}},
                        ensure_ascii=False,
                    )
                else:
                    payload = json.dumps(
                        {"tool_result": {"name": name, "output": raw[:1000]}},
                        ensure_ascii=False,
                    )
                yield f"data: {payload}\n\n"

            # ── 压缩结果 ──────────────────────────────────────────────────────
            elif event_type == "on_chain_end" and event.get("name") == "compress_memory":
                output = event["data"].get("output", {})
                if isinstance(output, dict):
                    compressed = output.get("compressed", False)

    except asyncio.CancelledError:
        # 客户端主动断开，静默退出即可
        logger.info("SSE 连接已断开 conv=%s", conv_id)
        return
    except Exception as exc:
        logger.error("图执行失败 conv=%s: %s", conv_id, exc, exc_info=True)
        yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"

    # ── 完成信号 ──────────────────────────────────────────────────────────────
    yield f"data: {json.dumps({'done': True, 'compressed': compressed})}\n\n"
