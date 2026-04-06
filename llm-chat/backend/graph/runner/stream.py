"""
stream_response：SSE 流驱动主函数

架构：
  1. 初始化 StreamContext 和 initial_state
  2. 启动后台图执行任务（_graph_producer），将 LangGraph 事件放入队列
  3. 启动心跳任务（_heartbeat），防止 nginx proxy_read_timeout 断流
  4. 主循环从队列消费事件，通过 EventDispatcher 转换为 SSE 字符串
  5. 图执行完成后推送 done 事件

错误处理：
  - 客户端断开时图 Task 被取消，主循环捕获后退出，不发 done
  - 图执行异常时通过 error 事件通知前端
"""
import asyncio
import logging
from typing import AsyncGenerator

from graph.agent import get_graph, get_simple_graph
from graph.runner.context import StreamContext
from graph.runner.dispatcher import dispatcher
from graph.runner.utils import sse
from graph.state import GraphState

logger = logging.getLogger("graph.runner.stream")


async def stream_response(
    conv_id: str,
    user_message: str,
    model: str,
    temperature: float = 0.7,
    client_id: str = "",
    images: list[str] | None = None,
    agent_mode: bool = True,
) -> AsyncGenerator[str, None]:
    """
    驱动 LangGraph 图执行，将事件流翻译为 FastAPI SSE 字符串流。

    agent_mode=True  → 完整图（planner + reflector + router，适合复杂任务）
    agent_mode=False → 简单图（直接对话 + 工具，适合普通问答）

    设计要点：
      - 图执行在独立 Task 中运行，主循环通过 asyncio.Queue 接收事件
      - 心跳任务每 5s 向队列注入 ping，主循环 yield 给前端，防止 nginx 超时断流
      - recursion_limit=120：支持最多约 28 个计划步骤（每步 4 节点 + 固定开销 5）
      - 图 Task 取消时（客户端断开）主循环捕获 CancelledError 并退出，不发 done
    """
    graph = get_graph(model) if agent_mode else get_simple_graph(model)
    ctx   = StreamContext(active_model=model)

    # 简单图跳过 route_model LLM 调用，直接设 route="chat" 使用默认模型
    initial_route = "" if agent_mode else "chat"

    initial_state: GraphState = {
        "conv_id":           conv_id,
        "client_id":         client_id,
        "user_message":      user_message,
        "images":            images or [],
        "model":             model,
        "temperature":       temperature,
        "messages":          [],
        "long_term_memories": [],
        "forget_mode":       False,
        "full_response":     "",
        "compressed":        False,
        "route":             initial_route,
        "tool_model":        model,
        "answer_model":      model,
        # 语义缓存初始值
        "cache_hit":         False,
        "cache_similarity":  0.0,
        # 视觉理解初始值（由 VisionNode 写入）
        "vision_description": "",
        # 澄清问询初始值（由 SaveResponseNode 检测后写入）
        "needs_clarification": False,
        # 认知规划初始值
        "plan":               [],
        "current_step_index": 0,
        "step_iterations":    0,
        "reflector_decision": "",
        "reflection":         "",
    }

    # ── 后台任务：驱动图执行，把事件放入队列 ─────────────────────────────────
    queue: asyncio.Queue[tuple] = asyncio.Queue()

    async def _graph_producer() -> None:
        event_count = 0
        try:
            async for event in graph.astream_events(
                initial_state,
                version="v2",
                config={"recursion_limit": 120},
            ):
                event_count += 1
                etype = event.get("event", "")
                ename = event.get("name", "")
                enode = event.get("metadata", {}).get("langgraph_node", "")
                # 只记录关键节点事件，避免日志爆炸
                if etype in (
                    "on_chain_start", "on_chain_end",
                    "on_tool_start", "on_tool_end",
                    "on_chat_model_start", "on_chat_model_end",
                ):
                    logger.info(
                        "图事件 #%d | conv=%s | event=%s | node=%s | name=%s",
                        event_count, conv_id, etype, enode, ename,
                    )
                await queue.put(("event", event))
        except asyncio.CancelledError:
            logger.info(
                "图执行被取消（客户端断开） | conv=%s | events=%d",
                conv_id, event_count,
            )
        except Exception as exc:
            logger.error(
                "图执行失败 | conv=%s | events=%d | error=%s",
                conv_id, event_count, exc, exc_info=True,
            )
            try:
                await queue.put(("error", exc))
            except Exception:
                pass
        finally:
            logger.info("图执行结束 | conv=%s | total_events=%d", conv_id, event_count)
            try:
                await queue.put(("done", None))
            except Exception:
                pass

    # ── 后台任务：每 5s 发一次心跳，防止 nginx proxy_read_timeout 断流 ────────
    async def _heartbeat() -> None:
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(5)
                ping_count += 1
                logger.info("心跳发送 #%d | conv=%s", ping_count, conv_id)
                await queue.put(("ping", None))
        except asyncio.CancelledError:
            logger.info("心跳任务结束 | conv=%s | pings=%d", conv_id, ping_count)

    graph_task = asyncio.create_task(_graph_producer())
    hb_task    = asyncio.create_task(_heartbeat())

    logger.info(
        "SSE 流开始 | conv=%s | model=%s | message='%.60s'",
        conv_id, model, user_message,
    )
    graph_done = False
    try:
        while True:
            kind, _event = await queue.get()
            if kind == "event":
                async for chunk in dispatcher.dispatch(_event, ctx):
                    yield chunk
            elif kind == "ping":
                logger.info("心跳已发送到前端 | conv=%s", conv_id)
                yield sse({"ping": True})
            elif kind == "error":
                logger.error("SSE 收到图错误 | conv=%s | error=%s", conv_id, str(_event))
                yield sse({"error": str(_event)})
                # 继续等待 done 信号，确保 finally 正常执行
            elif kind == "done":
                graph_done = True
                break
    except asyncio.CancelledError:
        # 客户端断开：取消图执行和心跳
        logger.info("SSE 流被客户端取消 | conv=%s", conv_id)
        graph_task.cancel()
    finally:
        hb_task.cancel()
        if graph_done:
            yield sse({"done": True, "compressed": ctx.compressed})
            logger.info("SSE 流正常结束 | conv=%s | compressed=%s", conv_id, ctx.compressed)
        else:
            logger.info("SSE 流异常结束（未收到 done） | conv=%s", conv_id)
