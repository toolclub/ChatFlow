"""
StreamSession — DB-first 流式会话管理器

核心原则：所有 IO 产生的数据立即写 DB。内存只做当前 worker 内的推送缓存。

数据流：
  图产生事件 → 队列 → 消费者 → {写 DB + 推 SSE 给客户端}
  刷新恢复 → 从 DB event_log 读取 → 推 SSE 给客户端
  跨 worker → 任意 worker 都能从 DB 读到完整数据

写入时机：
  用户发消息   → 立即 INSERT messages (role=user)
  助手开始生成 → 立即 INSERT messages (role=assistant, stream_completed=false)
  thinking 增量 → 每 500ms UPDATE messages.thinking
  content 增量  → 每 500ms UPDATE messages.stream_buffer
  工具调用开始  → 立即 INSERT tool_executions (status=running)
  工具调用完成  → 立即 UPDATE tool_executions (status=done)
  每个 SSE 事件 → 批量 INSERT event_log（500ms 一批）
  消息完成     → UPDATE messages (content=最终, stream_completed=true)
"""
import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator

from graph.agent import get_graph, get_simple_graph
from graph.runner.context import StreamContext
from graph.runner.dispatcher import dispatcher
from graph.runner.utils import sse
from graph.state import GraphState

logger = logging.getLogger("graph.runner.stream")

# 进程内追踪活跃会话（仅用于本 worker 的 SSE 实时推送）
# 跨 worker 的活跃状态由 Redis 管理（db.redis_state）
_active_sessions: dict[str, "StreamSession"] = {}


def is_streaming_in_worker(conv_id: str) -> bool:
    """当前 worker 是否正在处理该对话的流式输出。"""
    return conv_id in _active_sessions


class StreamSession:
    """管理单个流式对话的完整生命周期（DB-first）。"""

    def __init__(
            
        self,
        conv_id: str,
        user_message: str,
        model: str,
        temperature: float = 0.7,
        client_id: str = "",
        images: list[str] | None = None,
        agent_mode: bool = True,
        force_plan: list[dict] | None = None,
        stop_event: asyncio.Event | None = None,
    ):
        self.conv_id = conv_id
        self.user_message = user_message
        self.model = model
        self.temperature = temperature
        self.client_id = client_id
        self.images = images or []
        self.agent_mode = agent_mode
        self.force_plan = force_plan or []
        self.stop_event = stop_event

        # 业务 ID
        self.assistant_message_id = str(uuid.uuid4())[:8]

        # DB 行 ID（写入后填充）
        self.user_db_id: int = 0
        self.assistant_db_id: int = 0

        # 对话状态机（持有实例，贯穿整个会话生命周期）
        from fsm.conversation import ConversationSM
        self._conv_sm = ConversationSM.from_db_status("active", conv_id=conv_id)

        # 内部状态
        self.queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.ctx = StreamContext(active_model=model)
        self._partial = ""
        self._streaming = ""
        self._save_done = False
        self._plan_id = ""
        self._step_idx = 0

        # SSE 事件缓冲（内存，用于当前连接的客户端推送）
        self._sse_events: list[str] = []
        self._sse_done = False
        self._sse_waiters: list[asyncio.Event] = []

        # DB 写入缓冲（批量写入 event_log）
        self._event_batch: list[dict] = []
        self._thinking_buf = ""
        self._content_buf = ""
        self._last_flush = time.time()
        self._tool_seq = 0

        # 思考段累积（spec「模型思考流程」协议）：
        # 按接收顺序追加；同 key 且紧邻（栈顶）才合并，否则开新段。
        # 这保证 retry、reasoning/content 交错等场景下时间顺序不丢失。
        self._thinking_segments: list[dict] = []
        self._thinking_dirty = False  # 累积后标记，_flush_to_db 检查是否需要写 DB

        # 工具调用 DB 持久化（支持多工具并行，按 seq 追踪每个工具的状态）
        self._tool_exec_map: dict[int, int] = {}   # tool_seq → tool_execution DB id
        self._tool_output_map: dict[int, str] = {}  # tool_seq → 累积的 sandbox 输出
        self._tool_search_map: dict[int, list] = {} # tool_seq → 搜索结果
        self._current_tool_seq: int = 0              # 当前活跃工具的 seq（最新 tool_call）

        # 消息终态化锁（防止 _periodic_flush 和 _finalize_message 竞态）
        self._finalize_lock = asyncio.Lock()
        self._finalized = False  # 终态标记，防止重复 finalize

        # 任务引用
        self._graph_task: asyncio.Task | None = None
        self._consumer_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None

    @property
    def best_partial(self) -> str:
        if self._save_done:
            return ""
        return self._streaming if self._streaming else self._partial

    # ══════════════════════════════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════════════════════════════

    async def stream(self) -> AsyncGenerator[str, None]:
        """启动会话并 yield SSE 事件。"""
        from memory import store as memory_store

        # ── 状态机驱动：active → streaming ──
        self._conv_sm.send_event("streaming")
        await memory_store.update_status(self.conv_id, self._conv_sm.current_value)

        try:
            self.user_db_id = await memory_store.create_message_immediate(
                self.conv_id, "user", self.user_message,
                images=self.images,
            )
            self.assistant_db_id = await memory_store.create_message_immediate(
                self.conv_id, "assistant", "",
                message_id=self.assistant_message_id,
                stream_completed=False,
            )
        except Exception as exc:
            logger.error("消息预写失败，中止流 | conv=%s | %s", self.conv_id, exc)
            yield sse({"error": "消息创建失败，请重试", "can_continue": False})
            yield sse({"done": True, "compressed": False})
            await memory_store.update_status(self.conv_id, "error")
            return

        # 写入 resume_context 事件（第一个事件，刷新时恢复用户消息）
        resume_sse = sse({"resume_context": {
            "user_message": self.user_message,
            "images": self.images,
        }})
        self._emit_sse(resume_sse, "resume_context")

        # 注册活跃会话（本 worker 内存 + Redis 跨 worker）
        _active_sessions[self.conv_id] = self
        try:
            from db.redis_state import register_streaming
            await register_streaming(self.conv_id)
        except Exception:
            pass  # Redis 不可用时降级到进程内 dict

        # 启动后台任务
        self._graph_task = asyncio.create_task(self._run_graph())
        self._hb_task = asyncio.create_task(self._heartbeat())
        self._consumer_task = asyncio.create_task(self._consume_events())
        self._flush_task = asyncio.create_task(self._periodic_flush())

        try:
            async for chunk in self._feed_client():
                yield chunk
        finally:
            pass  # 客户端断开，但 graph/consumer/flush 继续运行

    # ══════════════════════════════════════════════════════════════════════════
    # 图执行（生产者）
    # ══════════════════════════════════════════════════════════════════════════

    async def _run_graph(self) -> None:
        from sandbox.context import current_conv_id, current_message_id
        current_conv_id.set(self.conv_id)
        current_message_id.set(self.assistant_message_id)

        graph = get_graph(self.model) if self.agent_mode else get_simple_graph(self.model)
        state = self._build_initial_state()
        event_count = 0

        try:
            async for event in graph.astream_events(
                state, version="v2",
                config={"recursion_limit": 120, "configurable": {"conv_id": self.conv_id}},
            ):
                if self.stop_event and self.stop_event.is_set():
                    logger.info("停止信号 | conv=%s", self.conv_id)
                    break
                event_count += 1
                self._track_graph_event(event)
                await self.queue.put(("event", event))

            if self.stop_event and self.stop_event.is_set():
                await self.queue.put(("stopped", None))
            else:
                await self.queue.put(("done", None))

        except asyncio.CancelledError:
            logger.info("图取消 | conv=%s | events=%d", self.conv_id, event_count)
            await self.queue.put(("cancelled", None))

        except Exception as exc:
            logger.error("图异常 | conv=%s | %s", self.conv_id, exc, exc_info=True)
            await self.queue.put(("error", {"exc": exc, "can_continue": bool(self.best_partial)}))

    def _track_graph_event(self, event: dict) -> None:
        etype = event.get("event", "")
        ename = event.get("name", "")
        enode = event.get("metadata", {}).get("langgraph_node", "")

        if etype == "on_chain_end" and enode in ("call_model", "call_model_after_tool", "reflector"):
            output = event.get("data", {}).get("output", {})
            if isinstance(output, dict) and output.get("full_response"):
                self._partial = output["full_response"]
                self._streaming = ""
        if etype == "on_chain_start" and enode == "save_response":
            self._save_done = True
        if etype == "on_chain_end" and enode == "planner":
            p = event.get("data", {}).get("output", {})
            if isinstance(p, dict):
                self._plan_id = p.get("plan_id", "") or self._plan_id
                self._step_idx = p.get("current_step_index", self._step_idx)
        if etype == "on_chain_end" and enode == "reflector":
            r = event.get("data", {}).get("output", {})
            if isinstance(r, dict) and "current_step_index" in r:
                self._step_idx = r["current_step_index"]
        if etype == "on_custom_event" and ename == "llm_token" and enode in ("call_model", "call_model_after_tool"):
            data = event.get("data", {})
            if isinstance(data, dict) and data.get("content"):
                self._streaming += data["content"]
        if etype == "on_chain_start" and enode in ("call_model", "call_model_after_tool"):
            self._streaming = ""

    # ══════════════════════════════════════════════════════════════════════════
    # 事件消费者
    # ══════════════════════════════════════════════════════════════════════════

    async def _consume_events(self) -> None:
        from memory import store as memory_store

        try:
            while True:
                kind, payload = await self.queue.get()

                if kind == "event":
                    async for chunk in dispatcher.dispatch(payload, self.ctx):
                        self._emit_sse(chunk)
                        await self._track_sse_for_db(chunk)

                elif kind == "ping":
                    self._emit_sse(sse({"ping": True}), "ping")

                elif kind == "done":
                    await self._flush_to_db()
                    await self._finalize_message()
                    self._emit_sse(sse({"done": True, "compressed": self.ctx.compressed}), "done")
                    await self._set_done("completed")
                    break

                elif kind == "stopped":
                    await self._flush_to_db()
                    await self._save_partial()
                    await self._mark_stream_completed()  # 确保 stream_completed=True
                    self._emit_sse(sse({"stopped": True}), "stopped")
                    await self._set_done("active")
                    break

                elif kind == "cancelled":
                    await self._flush_to_db()
                    await self._mark_stream_completed()  # 确保 stream_completed=True
                    self._emit_sse(sse({"stopped": True}), "stopped")
                    await self._set_done("active")
                    break

                elif kind == "error":
                    await self._flush_to_db()
                    err = payload if isinstance(payload, dict) else {"exc": payload, "can_continue": False}
                    can_cont = err.get("can_continue", False)
                    if can_cont:
                        await self._save_partial()
                    self._emit_sse(sse({"error": str(err.get("exc", "")), "can_continue": can_cont}), "error")
                    self._emit_sse(sse({"done": True, "compressed": False}), "done")
                    await self._set_done("error")
                    break

        except asyncio.CancelledError:
            logger.info("消费者取消 | conv=%s", self.conv_id)
        except Exception as exc:
            logger.error("消费者异常 | conv=%s | %s", self.conv_id, exc, exc_info=True)
            # 消费者崩溃时也要通知前端，避免 SSE 流永远挂起
            try:
                self._emit_sse(sse({"error": f"内部错误: {exc}", "can_continue": bool(self.best_partial)}), "error")
                self._emit_sse(sse({"done": True, "compressed": False}), "done")
            except Exception:
                pass
        finally:
            # 确保 _sse_done 被设置（即使异常路径也能让 _feed_client 退出）
            if not self._sse_done:
                self._sse_done = True
                for w in self._sse_waiters:
                    w.set()
            if self._graph_task and not self._graph_task.done():
                self._graph_task.cancel()
            if self._hb_task:
                self._hb_task.cancel()
            if self._flush_task:
                self._flush_task.cancel()
            # 只有当 _active_sessions 中还是自己时才移除（避免新 session 被老 session 误删）
            if _active_sessions.get(self.conv_id) is self:
                _active_sessions.pop(self.conv_id, None)

    # ══════════════════════════════════════════════════════════════════════════
    # SSE 推送 + DB 写入
    # ══════════════════════════════════════════════════════════════════════════

    def _emit_sse(self, sse_str: str, event_type: str = "") -> None:
        """推送 SSE 事件到内存缓冲 + 加入 DB 写入批次。"""
        self._sse_events.append(sse_str)
        for w in self._sse_waiters:
            w.set()

        # 加入 DB 写入批次（ping 不写 DB）
        from db.state_machine import SSEEventType
        if event_type != SSEEventType.PING.value and sse_str:
            self._event_batch.append({
                "conv_id": self.conv_id,
                "message_id": self.assistant_message_id,
                "event_type": event_type or self._detect_event_type(sse_str),
                "sse_string": sse_str,
            })

    def _append_thinking_delta(
        self, node: str, step_index: int | None, phase: str, delta: str,
    ) -> None:
        """
        将一个 thinking delta 追加到 segments 列表。
        栈顶段 key 相同 → 拼接到同段；否则开新段。
        保证时间顺序严格 = SSE 到达顺序，retry/交错不会错位合并。
        """
        top = self._thinking_segments[-1] if self._thinking_segments else None
        if top and top["node"] == node and top["step_index"] == step_index and top["phase"] == phase:
            top["content"] += delta
        else:
            self._thinking_segments.append({
                "node": node, "step_index": step_index,
                "phase": phase, "content": delta,
            })
        self._thinking_buf += delta  # 向后兼容：拼接纯文本
        self._thinking_dirty = True

    async def _track_sse_for_db(self, sse_str: str) -> None:
        """
        从 SSE 字符串提取 thinking/content/tool 用于定期刷新到 DB。

        工具调用持久化流程（确保刷新后能恢复 sandbox 终端）：
          tool_call   → 立即 INSERT tool_executions (status=running)
          sandbox_output → 累积到 _sandbox_output_buf（终端过程输出）
          tool_result → UPDATE tool_executions (status=done, output=累积的终端输出)
        """
        if not sse_str.startswith("data: "):
            return
        try:
            data = json.loads(sse_str[6:].strip())
        except (json.JSONDecodeError, ValueError):
            return

        # thinking：结构化 payload（spec 协议），按接收顺序追加成段。
        # 合并规则：仅当**栈顶段** key 与新 delta 相同时才拼接；否则开新段。
        # 这样 retry、reasoning/content 交错都不会丢失时间顺序。
        thinking_payload = data.get("thinking")
        if isinstance(thinking_payload, dict):
            delta = thinking_payload.get("delta", "")
            if delta:
                node = thinking_payload.get("node", "")
                step_index = thinking_payload.get("step_index")
                phase = thinking_payload.get("phase", "reasoning")
                self._append_thinking_delta(node, step_index, phase, delta)
        elif isinstance(thinking_payload, str) and thinking_payload:
            # COMPAT: 裸字符串 thinking（legacy/异常路径），归入未知节点段
            self._append_thinking_delta("", None, "reasoning", thinking_payload)
        if data.get("content"):
            self._content_buf += data["content"]

        # ── 工具调用开始 → 创建 tool_execution 记录（支持多工具并行） ──
        if data.get("tool_call"):
            tc = data["tool_call"]
            self._tool_seq += 1
            seq = self._tool_seq
            self._current_tool_seq = seq
            self._tool_output_map[seq] = ""
            self._tool_search_map[seq] = []
            try:
                from db.tool_store import create_tool_execution
                exec_id = await create_tool_execution(
                    conv_id=self.conv_id,
                    message_id=self.assistant_message_id,
                    tool_name=tc.get("name", ""),
                    tool_input=tc.get("input", {}),
                    sequence_number=seq,
                    step_index=self._step_idx if self._plan_id else None,
                )
                self._tool_exec_map[seq] = exec_id
            except Exception as exc:
                logger.warning("tool_execution 创建失败: %s", exc)

        # ── sandbox 终端输出 → 累积到当前活跃工具 ──
        if data.get("sandbox_output"):
            so = data["sandbox_output"]
            seq = self._current_tool_seq
            if seq in self._tool_output_map:
                self._tool_output_map[seq] += so.get("text", "")

        # ── 搜索结果 → 累积到当前活跃工具 ──
        if data.get("search_item"):
            si = data["search_item"]
            seq = self._current_tool_seq
            if seq in self._tool_search_map:
                self._tool_search_map[seq].append({
                    "url": si.get("url", ""),
                    "title": si.get("title", ""),
                    "status": si.get("status", "done"),
                })

        # ── 文件产物 → 立即保存到 artifacts 表（兜底，工具内可能已保存） ──
        if data.get("file_artifact"):
            fa = data["file_artifact"]
            try:
                from db.artifact_store import save_artifact
                # file_artifact 事件中只存元数据（binary 内容由工具内的 save_artifact 保存）
                # 这里是兜底：如果工具内没保存，至少保存一次
                if not fa.get("binary"):
                    await save_artifact(
                        self.conv_id,
                        fa.get("name", ""),
                        fa.get("path", ""),
                        fa.get("content", ""),
                        fa.get("language"),
                    )
            except Exception as exc:
                logger.warning("artifact 兜底保存失败: %s", exc)

        # ── 工具调用结束 → 按 seq 匹配对应工具，写入 DB ──
        if data.get("tool_result"):
            tr = data["tool_result"]
            seq = self._current_tool_seq
            exec_id = self._tool_exec_map.get(seq, 0)
            if exec_id:
                output = self._tool_output_map.get(seq, "") or tr.get("output", "")
                search_items = self._tool_search_map.get(seq) or None
                # 状态机驱动：running → done/error/timeout
                from fsm.tool_execution import ToolExecutionSM
                raw_status = tr.get("status", "done")
                tool_sm = ToolExecutionSM()
                tool_sm.send_event(raw_status)
                status = tool_sm.current_value
                try:
                    from db.tool_store import complete_tool_execution
                    await complete_tool_execution(
                        exec_id,
                        output=output[:20000],
                        status=status,
                        search_items=search_items,
                    )
                except Exception as exc:
                    logger.warning("tool_execution 完成失败: %s", exc)
                # 清理已完成工具的缓冲
                self._tool_exec_map.pop(seq, None)
                self._tool_output_map.pop(seq, None)
                self._tool_search_map.pop(seq, None)

    @staticmethod
    def _detect_event_type(sse_str: str) -> str:
        """
        从 SSE 字符串推断事件类型。

        使用 SSEEventType 注册表（单一真相源），按优先级匹配，
        控制事件 > 工具事件 > 内容事件，避免多 key 共存时误判。
        """
        if not sse_str.startswith("data: "):
            return "unknown"
        try:
            data = json.loads(sse_str[6:].strip())
        except Exception:
            return "unknown"
        from db.state_machine import detect_sse_event_type
        return detect_sse_event_type(data).value

    async def _periodic_flush(self) -> None:
        """每 500ms 刷新累积数据到 DB。"""
        try:
            while True:
                await asyncio.sleep(0.5)
                await self._flush_to_db()
        except asyncio.CancelledError:
            await self._flush_to_db()

    async def _flush_to_db(self) -> None:
        """将累积的事件批量写入 event_log，并更新 messages 的 thinking/buffer。"""
        # event_log 始终写入（包括终态化后的 done/stopped 事件），不受 _finalized 限制
        if self._event_batch:
            batch = self._event_batch[:]
            self._event_batch.clear()
            try:
                from db.event_store import append_events_batch
                await append_events_batch(batch)
            except Exception as exc:
                logger.warning("event_log 批量写入失败: %s", exc)

        # messages 表更新：终态化后跳过（防止覆盖最终内容）
        async with self._finalize_lock:
            if self._finalized:
                return

            if (self._thinking_buf or self._content_buf or self._thinking_dirty) and self.assistant_db_id:
                try:
                    from memory import store as memory_store
                    await memory_store.update_message_streaming(
                        self.assistant_db_id,
                        thinking=self._thinking_buf if self._thinking_buf else None,
                        stream_buffer=self._content_buf if self._content_buf else None,
                        thinking_segments=list(self._thinking_segments) if self._thinking_dirty else None,
                    )
                    self._thinking_dirty = False
                except Exception as exc:
                    logger.warning("消息流式更新失败: %s", exc)

    async def _finalize_message(self) -> None:
        """消息生成完成：写入最终内容，标记终态，防止后续 flush 覆盖。"""
        if not self.assistant_db_id:
            return
        async with self._finalize_lock:
            if self._finalized:
                return  # 幂等：已终态化则跳过
            self._finalized = True

            # save_response_node 已经通过 add_message 保存了最终内容到 messages 表
            # 这里确保 stream_completed=True、thinking 字段、清空 stream_buffer
            try:
                from memory import store as memory_store
                await memory_store.update_message_streaming(
                    self.assistant_db_id,
                    thinking=self._thinking_buf or None,
                    thinking_segments=list(self._thinking_segments) if self._thinking_segments else None,
                    stream_buffer="",  # 显式清空
                    stream_completed=True,
                )
            except Exception as exc:
                logger.warning("_finalize_message 写 DB 失败: %s", exc)

    async def _save_partial(self) -> None:
        """保存部分响应（用户停止或异常时）。"""
        content = self.best_partial
        # COMPAT: legacy think block parsing — 保存部分响应时移除 <think> 标签。
        # 待模型 API 统一支持 reasoning_content 结构化字段后可移除。
        stripped = re.sub(r"<think>[\s\S]*?</think>", "", content) if content else ""
        stripped = re.sub(r"<think>[\s\S]*$", "", stripped).strip() if stripped else ""
        if not stripped:
            return
        try:
            from memory import store as memory_store
            tag = "\n\n[回复中断，以上为部分结果。用户可能要求继续。]"
            await memory_store.finalize_message(
                self.assistant_db_id,
                content=stripped + tag,
                thinking=self._thinking_buf,
                thinking_segments=list(self._thinking_segments) if self._thinking_segments else None,
            )
        except Exception as exc:
            logger.warning("保存部分响应失败: %s", exc)

    async def _mark_stream_completed(self) -> None:
        """强制标记 assistant 消息 stream_completed=True（stopped/cancelled 时调用）。"""
        if not self.assistant_db_id:
            return
        try:
            from memory import store as memory_store
            await memory_store.update_message_streaming(
                self.assistant_db_id, stream_buffer="", stream_completed=True,
            )
        except Exception as exc:
            logger.warning("_mark_stream_completed 失败: %s", exc)

    async def _set_done(self, status: str) -> None:
        """通过状态机驱动会话结束：streaming → completed/error/active。"""
        self._sse_done = True
        for w in self._sse_waiters:
            w.set()
        try:
            self._conv_sm.send_event(status)
            from memory import store as memory_store
            await memory_store.update_status(self.conv_id, self._conv_sm.current_value)
        except Exception as exc:
            logger.warning("_set_done 状态持久化失败: %s", exc)
        # 注销 Redis 活跃会话
        try:
            from db.redis_state import unregister_streaming
            await unregister_streaming(self.conv_id)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # 心跳 + 客户端推送
    # ══════════════════════════════════════════════════════════════════════════

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(3)
                await self.queue.put(("ping", None))
                # Redis 活跃会话续期 + 停止信号检测（带 2 秒超时，防止 Redis 慢时阻塞心跳）
                try:
                    from db.redis_state import heartbeat_streaming, check_stop
                    await asyncio.wait_for(heartbeat_streaming(self.conv_id), timeout=2)
                    if await asyncio.wait_for(check_stop(self.conv_id), timeout=2):
                        if self.stop_event:
                            self.stop_event.set()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _feed_client(self) -> AsyncGenerator[str, None]:
        """从内存 SSE 缓冲推送给当前连接的客户端。"""
        idx = 0
        waiter = asyncio.Event()
        self._sse_waiters.append(waiter)
        try:
            while True:
                if idx < len(self._sse_events):
                    for evt in self._sse_events[idx:]:
                        yield evt
                    idx = len(self._sse_events)
                if self._sse_done:
                    for evt in self._sse_events[idx:]:
                        yield evt
                    break
                waiter.clear()
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("客户端断开 | conv=%s", self.conv_id)
        finally:
            try:
                self._sse_waiters.remove(waiter)
            except ValueError:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════════════════════════

    def _build_initial_state(self) -> GraphState:
        return {
            "conv_id": self.conv_id, "client_id": self.client_id,
            "user_message": self.user_message, "images": self.images,
            "model": self.model, "temperature": self.temperature,
            "messages": [], "long_term_memories": [], "forget_mode": False,
            "full_response": "", "compressed": False,
            "route": "" if self.agent_mode else "chat",
            "tool_model": self.model, "answer_model": self.model,
            "cache_hit": False, "cache_similarity": 0.0,
            "vision_description": "", "needs_clarification": False, "clarification_data": {},
            "pre_user_db_id": self.user_db_id,
            "pre_assistant_db_id": self.assistant_db_id,
            "assistant_message_id": self.assistant_message_id,
            "force_plan": self.force_plan, "plan": [], "plan_id": "",
            "plan_goal": "", "current_step_index": 0,
            "step_iterations": 0, "reflector_decision": "",
            "reflection": "", "step_results": [],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 公共接口
# ═══════════════════════════════════════════════════════════════════════════════

async def stream_response(
    conv_id: str, user_message: str, model: str,
    temperature: float = 0.7, client_id: str = "", images: list[str] | None = None,
    agent_mode: bool = True, force_plan: list[dict] | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[str, None]:
    session = StreamSession(
        conv_id=conv_id, user_message=user_message, model=model,
        temperature=temperature, client_id=client_id, images=images,
        agent_mode=agent_mode, force_plan=force_plan, stop_event=stop_event,
    )
    async for chunk in session.stream():
        yield chunk


async def resume_stream(conv_id: str, after_event_id: int = 0, message_id: str = "") -> AsyncGenerator[str, None]:
    """
    恢复流式输出（DB-first 版）：从 event_log 读取历史事件 + 实时事件。

    1. 从 event_log WHERE id > after_event_id 读取遗漏事件
    2. 如果当前 worker 正在处理该对话，切换到实时推送
    3. 如果不是当前 worker，只能返回 DB 中已有的事件

    message_id: 限定只回放指定 assistant message 的事件（多轮对话时避免混入旧轮）。
    """
    from db.event_store import get_events_since

    # 从 DB 读取遗漏的事件（按 message_id 过滤，避免多轮事件混淆）
    events = await get_events_since(conv_id, after_event_id, message_id=message_id)
    for evt in events:
        if evt["sse_string"]:
            yield evt["sse_string"]

    # 检查是否还有终止事件
    has_terminal = any(e["event_type"] in ("done", "stopped", "error") for e in events)
    if has_terminal:
        return

    # 如果当前 worker 正在处理，切换到实时推送
    session = _active_sessions.get(conv_id)
    if session:
        # 从 session 的内存缓冲继续（跳过已从 DB 发过的）
        idx = len(session._sse_events)  # 从当前位置开始
        waiter = asyncio.Event()
        session._sse_waiters.append(waiter)
        try:
            while True:
                if idx < len(session._sse_events):
                    for evt in session._sse_events[idx:]:
                        yield evt
                    idx = len(session._sse_events)
                if session._sse_done:
                    for evt in session._sse_events[idx:]:
                        yield evt
                    break
                waiter.clear()
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            try:
                session._sse_waiters.remove(waiter)
            except ValueError:
                pass
    else:
        # 不在当前 worker — 轮询 DB（不理想但能用）
        last_id = events[-1]["id"] if events else after_event_id
        for _ in range(600):  # 最多 5 分钟
            await asyncio.sleep(0.5)
            new_events = await get_events_since(conv_id, last_id)
            for evt in new_events:
                if evt["sse_string"]:
                    yield evt["sse_string"]
                last_id = evt["id"]
            if any(e["event_type"] in ("done", "stopped", "error") for e in new_events):
                break
