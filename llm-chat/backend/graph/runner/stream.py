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
import re
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
    force_plan: list[dict] | None = None,
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
        "force_plan":         force_plan or [],
        "plan":               [],
        "plan_id":            "",
        "plan_goal":          "",
        "current_step_index": 0,
        "step_iterations":    0,
        "reflector_decision": "",
        "reflection":         "",
        "step_results":       [],
    }

    # ── 后台任务：驱动图执行，把事件放入队列 ─────────────────────────────────
    queue: asyncio.Queue[tuple] = asyncio.Queue()

    # 用可变容器在 _graph_producer 和外层主循环之间共享已累积的响应内容。
    # 当客户端断开（CancelledError）时，外层可读取它并保存到 DB。
    _partial: list[str] = [""]
    # 实时流式 token 累积：_stream_tokens 通过 on_custom_event("llm_token") 逐 token 派发，
    # 在节点完成前（on_chain_end 之前）就能捕获已生成的内容。
    # 当连接断开时，_streaming[0] 包含比 _partial[0] 更新鲜的部分内容。
    _streaming: list[str] = [""]
    # save_response 节点是否已执行：若已执行则不做 partial save（避免重复写入）
    _saved: list[bool] = [False]
    # 追踪当前执行计划的 ID 和步骤索引（用于崩溃时将部分内容写入 plan step DB）
    _plan_id: list[str] = [""]
    _step_idx: list[int] = [0]
    _plan_len: list[int] = [0]

    def _best_partial() -> str:
        """选择最优的部分响应用于保存：优先使用实时流式累积（更完整），回退到节点输出。"""
        if _saved[0]:
            return ""  # save_response 已执行，不需要 partial save
        return _streaming[0] if _streaming[0] else _partial[0]

    async def _graph_producer() -> None:
        # 设置沙箱上下文（供 sandbox tools 读取 conv_id）
        from sandbox.context import current_conv_id
        current_conv_id.set(conv_id)

        event_count = 0
        try:
            async for event in graph.astream_events(
                initial_state,
                version="v2",
                config={
                    "recursion_limit": 120,
                    "configurable": {"conv_id": conv_id},
                },
            ):
                event_count += 1
                etype = event.get("event", "")
                ename = event.get("name", "")
                enode = event.get("metadata", {}).get("langgraph_node", "")
                # 追踪 LLM 节点输出，供中断时保存
                if etype == "on_chain_end" and enode in ("call_model", "call_model_after_tool"):
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        fr = output.get("full_response", "")
                        if fr:
                            _partial[0] = fr
                            # 节点完成后清空流式缓冲（_partial 已是完整内容）
                            _streaming[0] = ""

                # 追踪 save_response 节点，防止 partial save 重复写入
                if etype == "on_chain_start" and enode == "save_response":
                    _saved[0] = True

                # ── 追踪计划 ID 和步骤索引（用于崩溃时写入 plan step DB）────
                if etype == "on_chain_end" and enode == "planner":
                    p_output = event.get("data", {}).get("output", {})
                    if isinstance(p_output, dict):
                        pid = p_output.get("plan_id", "")
                        if pid:
                            _plan_id[0] = pid
                        _step_idx[0] = p_output.get("current_step_index", 0)
                        _plan_len[0] = len(p_output.get("plan", []))
                if etype == "on_chain_end" and enode == "reflector":
                    r_output = event.get("data", {}).get("output", {})
                    if isinstance(r_output, dict):
                        if "current_step_index" in r_output:
                            _step_idx[0] = r_output["current_step_index"]

                # ── 实时追踪流式 token（断点续传核心） ────────────────────────
                # _stream_tokens 通过 adispatch_custom_event("llm_token") 派发 token，
                # 这里同步累积到 _streaming[0]，确保连接断开时有最新的部分内容。
                if etype == "on_custom_event" and ename == "llm_token" and enode in ("call_model", "call_model_after_tool"):
                    data = event.get("data", {})
                    if isinstance(data, dict):
                        token = data.get("content", "")
                        if token:
                            _streaming[0] += token
                # 新 LLM 节点开始时重置流式缓冲（上一个节点的内容已在 _partial 中）
                if etype == "on_chain_start" and enode in ("call_model", "call_model_after_tool"):
                    _streaming[0] = ""

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
            # 客户端断开：用 shield 异步保存已积累的部分响应（不受取消影响）
            content = _best_partial()
            logger.info(
                "图执行被取消（客户端断开） | conv=%s | events=%d | partial_len=%d | streaming_len=%d",
                conv_id, event_count, len(_partial[0]), len(_streaming[0]),
            )
            try:
                await asyncio.shield(_save_partial_response(conv_id, user_message, content))
                await asyncio.shield(_save_partial_plan_step(_plan_id[0], _step_idx[0], content))
            except Exception as save_exc:
                logger.warning("保存断开前部分响应失败: %s", save_exc)
        except Exception as exc:
            logger.error(
                "图执行失败 | conv=%s | events=%d | error=%s",
                conv_id, event_count, exc, exc_info=True,
            )
            # 若已有部分响应（如 GraphRecursionError 中断），保存到 DB
            # 这样用户说"继续"时，模型能从历史中知道之前做到了哪里
            content = _best_partial()
            can_continue = bool(content)
            if can_continue:
                await _save_partial_response(conv_id, user_message, content)
                # 同时将部分内容写入 plan step DB（断点续传时 _build_focused_step_messages 需要）
                await _save_partial_plan_step(_plan_id[0], _step_idx[0], content)
            try:
                await queue.put(("error", {"exc": exc, "can_continue": can_continue}))
            except Exception:
                pass
        finally:
            logger.info("图执行结束 | conv=%s | total_events=%d", conv_id, event_count)
            try:
                await queue.put(("done", None))
            except Exception:
                pass

    # ── 后台任务：每 3s 发一次心跳，防止 nginx proxy_read_timeout / 前端 idle 断流 ──
    async def _heartbeat() -> None:
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(3)
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
                err_info = _event if isinstance(_event, dict) else {"exc": _event, "can_continue": False}
                logger.error("SSE 收到图错误 | conv=%s | error=%s", conv_id, str(err_info.get("exc")))
                yield sse({
                    "error": str(err_info.get("exc", _event)),
                    "can_continue": err_info.get("can_continue", False),
                })
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


async def _save_partial_response(conv_id: str, user_message: str, full_response: str) -> None:
    """
    图执行异常中断（如 GraphRecursionError / 客户端断开）时，将已生成的部分响应保存到数据库。

    保存后，用户说"继续"时模型能从历史中感知到之前做到了哪里，实现真正的断点续接。
    若 full_response 去除 think 块后为空则不保存（避免写入脏数据）。

    支持两种 think 块情况：
      - 完整 <think>...</think> 块：正常移除
      - 不完整 <think>... 块（流式中断）：移除到末尾
    """
    # 先移除完整 think 块
    stripped = re.sub(r"<think>[\s\S]*?</think>", "", full_response) if full_response else ""
    # 再移除未闭合的 think 块（流式中断时可能出现 <think>... 没有 </think>）
    stripped = re.sub(r"<think>[\s\S]*$", "", stripped).strip() if stripped else ""
    try:
        from memory import store as memory_store
        # 始终保存用户消息（确保"继续"时模型能看到原始问题）
        await memory_store.add_message(conv_id, "user", user_message.replace("\x00", ""))
        # 有部分响应时同时保存（附带续传标记）
        if stripped:
            content_to_save = stripped + "\n\n[回复中断，以上为部分结果。用户可能要求继续，请从中断处接着输出，不要重复已有内容。]"
            await memory_store.add_message(conv_id, "assistant", content_to_save.replace("\x00", ""))
        logger.info(
            "已保存中断前的部分响应 | conv=%s | response_len=%d", conv_id, len(stripped)
        )
    except Exception as exc:
        logger.warning("保存部分响应失败（不影响主流程）: %s", exc)


async def _save_partial_plan_step(plan_id: str, step_idx: int, content: str) -> None:
    """
    崩溃时将部分内容写入 plan step DB 的 result 字段。

    断点续传时 _build_focused_step_messages 从 plan[i].result 构建上下文，
    若不保存到 plan step，续传时模型看不到已生成的部分内容，会从头开始。
    """
    if not plan_id or not content:
        return
    # 清理 think 块
    stripped = re.sub(r"<think>[\s\S]*?</think>", "", content)
    stripped = re.sub(r"<think>[\s\S]*$", "", stripped).strip()
    if not stripped:
        return
    try:
        from db.plan_store import save_step_result
        await save_step_result(plan_id, step_idx, stripped, step_idx)  # next_step = 当前步（未完成）
        logger.info(
            "已保存中断步骤的部分结果到 plan DB | plan_id=%s | step=%d | len=%d",
            plan_id, step_idx, len(stripped),
        )
    except Exception as exc:
        logger.warning("保存中断步骤结果到 plan DB 失败: %s", exc)
