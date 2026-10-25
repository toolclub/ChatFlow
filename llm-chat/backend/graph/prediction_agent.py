"""股票预测 LangGraph 编排 — 后台图 + event_log 增量持久化 + SSE 重连恢复。

镜像 graph/runner/stream.py 的 chat resume 模式（slim port，非 StreamSession 全量克隆）：
  - 图作为后台 fire-and-forget 任务运行（survives SSE disconnect）
  - 每个 SSE 事件增量持久化到 event_log（conv_id=prediction_id，真相源）
  - 每 3s 心跳 ping（不写 event_log！否则会攒下几千条垃圾行并在每次重连时全量重放）
  - /stream 重连：先 replay event_log（after_event_id 之后），再 drain 实时队列
  - worker 重启（session 丢失）+ 非终态：重置 queued 重跑

图拓扑（契约 E，「失败要早」）：
    START → preflight → (state.error ? END : news_search) → compute
          → (state.error ? END : analyze) → END
  预检把「这只票拉不到行情」这种 2 秒就能判死的失败提到最前面 —— 原先它排在 11~16 分钟的
  资讯搜索之后；compute 失败也不再白烧一次 analyze 的 LLM（用户先看到分析流出来再弹红错）。

设计取舍：
  - background_predict：fire-and-forget 兜底路径（非 SSE 场景，ainvoke 一次性完成）
  - stream_predict：PRIMARY 路径，replay event_log → drain 队列 / 重跑

铁律：
  - #3 事件字符串用 SSEEventType.*.value（不用 "delta"/"thinking" 字面量）
  - #4 DB 操作全 async（AsyncSessionLocal，在 async def 内）
  - #5 LLM 调用 astream（在节点 + news_search helpers 内）
  - #6 终态转换先 PredictionSM.send_event 再 update_prediction_status
  - #7 thinking 经 BaseNode.emit_thinking
  - #9 异常不吞
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from db.event_store import append_event, get_events_since
from db.prediction_view import get_compact_prediction_events
from db.prediction_store import (
    get_prediction_snapshot,
    get_prediction_runtime_snapshot,
    update_prediction_analysis,
    update_prediction_progress,
    update_prediction_status,
)
from fsm.prediction import PredictionSM
from fsm.sse_events import SSEEventType
from graph.nodes.prediction import (
    AnalyzeNode,
    ComputeNode,
    NewsSearchNode,
    PreflightNode,
)
from graph.nodes.prediction.news_search_node import resolve_dimensions
from graph.prediction.config import PREDICTION_HORIZON_DAYS
from graph.prediction import runtime as prediction_runtime

logger = logging.getLogger("graph.prediction_agent")

__all__ = [
    "predict_graph",
    "PredictState",
    "background_predict",
    "stream_predict",
    "start_prediction_graph",
    "stop_prediction",
    "pause_prediction",
    "resume_prediction",
    "remaining_dimensions",
    "discard_session",
    "shutdown_prediction_sessions",
    "build_initial_state",
    "HEARTBEAT_INTERVAL_SECONDS",
]

# 终态集合（paused **不在**其中：它可 resume 续跑，见 fsm/prediction.py）
_TERMINAL_STATUSES = ("done", "failed", "stopped")
# 非终态集合
_RUNNING_STATUSES = ("queued", "news_searching", "inferring")
# 暂停态（非终态，但对**当前这条 SSE 流**是终点：后台图已被 cancel，不会再有事件）
_PAUSED = "paused"

# 取消原因：区分「用户点停止」（→ STOPPED，终态）、「用户点暂停」（→ PAUSED，可续跑）
# 和「整单删除」（什么都不发、什么都不写 —— 那一行马上就没了）。
_CANCEL_STOPPED = "stopped"
_CANCEL_PAUSED = "paused"
_CANCEL_DELETED = "deleted"
_CANCEL_LEASE_LOST = "lease_lost"

# 对**一条 SSE 流**而言的终点事件。paused 在状态机里不是终态（可 resume），但对这条流是：
# 后台图已被 cancel，不会再有任何事件。不把它算进来的话，replay 完 paused 事件后会继续往下
# 走"无 session + 非终态 → 重跑"的分支，用户点的暂停会被 /stream 自动撤销。
_STREAM_TERMINAL_EVENTS = (
    SSEEventType.DONE.value,
    SSEEventType.ERROR.value,
    SSEEventType.STOPPED.value,
    SSEEventType.PAUSED.value,
)

# 心跳间隔（秒）。与 chat 的 graph/runner/stream.py:665 保持一致：3s 一个 ping，
# 保证 SSE 链路上不会出现 >15s 的零字节空档（实测修复前最长 198s，用户以为卡死）。
HEARTBEAT_INTERVAL_SECONDS = 3.0
# drain 队列的轮询超时：超时即补发一个 ping（心跳任务万一挂了也有兜底）
_DRAIN_POLL_TIMEOUT_SECONDS = 5.0


async def _transition_status(
    snapshot_id: str, target: str, error_message: str | None = None,
    *, allow_resume: bool = False,
) -> bool:
    """Validate every transition; never force an old graph over pause/stop.

    The store repeats validation under a row lock to reject stale node writes.
    When a completed graph missed an intermediate write, follow the legal FSM
    path to completion rather than bypassing the state machine.
    """
    try:
        cur = await get_prediction_snapshot(snapshot_id)
        if cur is None:
            return False
        current = cur.status or "queued"
        if current == target:
            return True
        if current in _TERMINAL_STATUSES:
            return False
        if current == _PAUSED and target != "stopped" and not allow_resume:
            return False
        path = [target]
        if target == "done" and current in ("queued", "news_searching"):
            path = (["news_searching"] if current == "queued" else []) + ["inferring", "done"]
        sm = PredictionSM.from_db_status(current)
        for step in path:
            if sm.send_event(step) != step:
                return False
            options = {"allow_resume": True} if allow_resume else {}
            changed = await update_prediction_status(
                snapshot_id, step, error_message=error_message, **options,
            )
            if changed is False:
                return False
        return True
    except Exception:
        logger.exception("预测状态转换失败 snap=%s target=%s", snapshot_id, target)
        return False


async def _finalize_progress(snapshot_id: str, error_kind: str = "") -> None:
    """终态收尾：清空阶段进度，顺带把错误类别落库。

    为什么要清空：progress 是"当前进行到哪一步"的快照，终态后若不清，记录列表会一直挂着
    「正在搜索：地缘政治 (3/12)」的残留标签（前端 hasPredictionProgress() 正是以 progress
    是否含 phase 来判定"有无进行中的阶段"）。

    为什么错误类别塞在这里：prediction_snapshots 没有 error_kind 列（schema 归 db/ 的 owner，
    本模块不改表），而前端要靠 kind 区分「限流/网络不通 → 稍后重试」和「代码写错了 → 改输入」。
    写成 {"error_kind": ...}（**不带 phase**）：hasPredictionProgress() 仍判为"无进度"，
    不会渲染残留标签；router 则从 progress 里把它提到响应的顶层 error_kind 字段。

    update_prediction_progress 内部保证不抛（铁律 #9 已在 store 层 logger.warning）。
    """
    await update_prediction_progress(
        snapshot_id, {"error_kind": error_kind} if error_kind else {},
    )


# ── 状态定义 ──────────────────────────────────────────────────────────────────

class PredictState(TypedDict, total=False):
    snapshot_id: str
    client_id: str
    user_id: str
    criteria: dict
    market: str
    symbol: str
    horizon: int
    dimensions: list[str] | None
    status: str
    # ★ 续跑标记：news_search 节点据此从 DB 读回已完成维度、只跑剩余的（设计文档 §7.2）。
    # 必须在此声明，否则 LangGraph 会静默丢弃这个 key（见下方 error_kind 的注释）。
    resume: bool
    news_digest: list[dict]
    news_sentiment: float
    # 数据型维度结果（FilingDimensionResult），compute 节点喂给 engine 算事件分 F
    filing_digest: list[dict]
    # 事件综合分 F ∈ [-1, 0]（compute 节点回填，来自 engine 的 model_params.filing_score）
    event_score: float
    bars_summary: dict
    forecast: dict
    analysis: str
    risk_notes: list[str]
    thinking_segments: list[dict]
    error: str
    # 错误类别（rate_limited/network/no_data/bad_symbol/news_unavailable）。
    # 必须在此声明：LangGraph 只把 TypedDict 里声明过的 key 写回 channel，
    # 节点返回的未声明 key 会被静默丢弃（langgraph/graph/state.py 的 `if k in output_keys`）。
    error_kind: str
    # PreflightNode 回填的行情预检结果 {"ok","symbol","market","bars","last_close","last_date"}
    preflight: dict


# ── 图构建（契约 E）───────────────────────────────────────────────────────────
#   START → preflight → (error ? END : news_search) → compute → (error ? END : analyze) → END

_preflight_node = PreflightNode()
_news_node = NewsSearchNode()
_compute_node = ComputeNode()
_analyze_node = AnalyzeNode()

# 图内节点名（on_chain_end 事件的 name 即取自这里；_run_prediction_graph 的白名单要与之一致）
_GRAPH_NODE_NAMES = ("preflight", "news_search", "compute", "analyze")


def _route_after_preflight(state: PredictState) -> str:
    """预检失败 → 直接 END：不再白跑 11~16 分钟的 12 维资讯搜索。"""
    if state.get("error"):
        logger.info(
            "预检失败短路到 END snap=%s kind=%s err=%s",
            state.get("snapshot_id", ""), state.get("error_kind", ""), state.get("error", ""),
        )
        return END
    return "news_search"


def _route_after_compute(state: PredictState) -> str:
    """compute 失败（行情/建模）→ 直接 END：不再多烧一次 analyze 的完整 LLM。

    news_search 全维失败也在这里收口 —— ComputeNode 入口自带 `if state.get("error")` 短路，
    error 原样透传到本条边。
    """
    if state.get("error"):
        logger.info(
            "compute 失败短路到 END snap=%s kind=%s err=%s",
            state.get("snapshot_id", ""), state.get("error_kind", ""), state.get("error", ""),
        )
        return END
    return "analyze"


def _build_predict_graph():
    builder = StateGraph(PredictState)
    builder.add_node("preflight", _preflight_node.execute)
    builder.add_node("news_search", _news_node.execute)
    builder.add_node("compute", _compute_node.execute)
    builder.add_node("analyze", _analyze_node.execute)

    builder.add_edge(START, "preflight")
    builder.add_conditional_edges(
        "preflight", _route_after_preflight, {"news_search": "news_search", END: END},
    )
    builder.add_edge("news_search", "compute")
    builder.add_conditional_edges(
        "compute", _route_after_compute, {"analyze": "analyze", END: END},
    )
    builder.add_edge("analyze", END)
    return builder.compile()


predict_graph = _build_predict_graph()


# ── 初始 state 构造（router 与重跑路径共用）──────────────────────────────────

def build_initial_state(
    snapshot_id: str,
    client_id: str,
    user_id: str,
    criteria: dict,
    status: str = "queued",
    resume: bool = False,
) -> PredictState:
    """从 criteria 字段构造 PredictState（router 层建行后调用，节点从 state 读字段）。

    resume=True：news_search 节点会先从 DB 读回已落库的维度结果，只跑剩余维度
    （续跑 / worker 重启重跑；见设计文档 §7.2）。dimensions 始终传**完整勾选集**，
    "跑哪些"由节点按 DB 里已完成的维度自行做差 —— 单一判据，两条路径不会算出不同答案。
    """
    criteria = criteria or {}
    return {
        "snapshot_id": snapshot_id,
        "client_id": client_id,
        "user_id": user_id,
        "criteria": criteria,
        "market": criteria.get("market", "cn_a"),
        "symbol": criteria.get("symbol", ""),
        "horizon": int(criteria.get("horizon") or PREDICTION_HORIZON_DAYS),
        "dimensions": criteria.get("dimensions"),
        "status": status,
        "resume": bool(resume),
    }


# ── 兜底：后台 fire-and-forget（非 SSE 场景）──────────────────────────────────

async def background_predict(
    snapshot_id: str,
    client_id: str,
    criteria: dict,
    user_id: str = "",
) -> None:
    """在后台运行预测管线（兜底路径；SSE 场景用 stream_predict + start_prediction_graph）。"""
    t0 = time.perf_counter()
    logger.info(
        "🚀 [后台预测] 启动 snapshot_id=%s client_id=%s user_id=%s",
        snapshot_id, client_id, user_id,
    )
    state = build_initial_state(snapshot_id, client_id, user_id, criteria)
    final: dict = {}
    try:
        final = await predict_graph.ainvoke(state) or {}
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("✅ [后台预测] 完成 snapshot_id=%s 耗时=%.0fms", snapshot_id, elapsed)
        # 节点已持久化 news/forecast；编排层补落 analysis + done（经 SM，铁律 #6）
        try:
            await update_prediction_analysis(
                snapshot_id,
                final.get("analysis", ""),
                final.get("risk_notes", []) or [],
                thinking_segments=final.get("thinking_segments", []),
            )
        except Exception as exc:
            logger.warning("后台预测回写 analysis 失败 snap=%s: %s", snapshot_id, exc)
        # 节点级 error（预检判死 / 资讯全挂 / forecast 失败）也必须落 failed —— 图短路到 END
        # 是"正常返回"，不会抛异常，只在 state 里留 error。
        err = final.get("error", "")
        if err:
            await _transition_status(snapshot_id, "failed", error_message=err)
            await _finalize_progress(snapshot_id, error_kind=final.get("error_kind", "") or "")
        else:
            await _transition_status(snapshot_id, "done")
            await _finalize_progress(snapshot_id)
    except Exception as exc:
        # 铁律 #9：异常不吞
        logger.exception("❌ [后台预测] 异常 snapshot_id=%s", snapshot_id)
        try:
            await _transition_status(snapshot_id, "failed", error_message=str(exc))
            await _finalize_progress(snapshot_id)
        except Exception as persist_exc:
            logger.warning("回写 failed 状态失败 snap=%s: %s", snapshot_id, persist_exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PRIMARY：后台图 + event_log 增量持久化 + SSE 重连恢复
# ═══════════════════════════════════════════════════════════════════════════════

# 进程内会话注册表（key=prediction_id）。
# 单 worker 本地 dev OK；event_log 是跨 worker / crash-recovery 的真相源。
_SESSIONS: dict[str, "PredictionSession"] = {}


def _ping_event() -> dict:
    """心跳事件（铁律 #3：类型取自 SSEEventType 枚举）。

    刻意不带 id：ping 不进 event_log，前端据此把它当作"只刷新停滞计时器、不推进游标"的信号。
    """
    return {"event": SSEEventType.PING.value}


class PredictionSession:
    """单次预测的后台会话：图任务 + 心跳任务 + 事件队列。

    图在后台 asyncio.Task 中运行，每个事件经 event_log 持久化（带自增 eid）后入队。
    /stream 消费者通过 drain() 实时拉取；eid 用于重连去重（skip 已 replay 的事件）。

    心跳（照抄 graph/runner/stream.py 的模式）：每 3s 往队列塞一个 ping，把 LLM 长思考 /
    Tavily 往返造成的 SSE 零字节空档填满，前端才能区分"还在跑"和"链路已死"。
    """

    def __init__(self, prediction_id: str):
        self.prediction_id = prediction_id
        # 队列项：(eid, ev) 或 None（终态哨兵）
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.changed = asyncio.Event()
        self.lease_token: str | None = None
        self.graph_task: asyncio.Task | None = None
        self.hb_task: asyncio.Task | None = None
        self.terminal: bool = False
        # 图被 cancel 时的原因：_CANCEL_STOPPED（用户停止，终态）/ _CANCEL_PAUSED（用户暂停，可续）。
        # _run_prediction_graph 的 CancelledError 分支据此决定发 STOPPED 还是 PAUSED 事件 ——
        # 光靠 CancelledError 分不出这两者，发错了前端就会把"暂停"渲染成"已停止"。
        self.cancel_reason: str = _CANCEL_STOPPED

    def start(self, graph_coro) -> None:
        """以后台任务启动图协程 + 心跳（均 survives SSE disconnect）。"""
        self.graph_task = asyncio.create_task(graph_coro)
        self.hb_task = asyncio.create_task(self._heartbeat())

    def enqueue(self, item) -> None:
        # This bounded local buffer is only a low-latency convenience. Public
        # streams read their own durable event_log cursor, so slow/disconnected
        # tabs cannot accumulate unbounded memory or steal one another's events.
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(item)
        self.changed.set()

    async def _heartbeat(self) -> None:
        """每 3s 往队列 put 一个 ping（镜像 graph/runner/stream.py:665-669）。

        ★关键：ping 绝不能走 _emit() —— 那会写 event_log。一次十几分钟的预测会攒下几百条
        垃圾 ping 行，并在每次重连 replay 时被全量重放。chat 侧 stream.py:384 同样在持久化
        批次里跳过 ping，这里用"直接 put 队列、绕开 _emit"达到同一效果。
        """
        try:
            while not self.terminal:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self.terminal:
                    break
                # eid=0：不在 event_log 中，drain 的 skip_eid 去重逻辑不会误跳过它
                self.enqueue((0, _ping_event()))
                if self.lease_token:
                    try:
                        if not await prediction_runtime.renew(self.prediction_id, self.lease_token):
                            raise RuntimeError("prediction ownership lease lost")
                        reason = await prediction_runtime.cancellation(self.prediction_id, self.lease_token)
                        if reason in (_CANCEL_PAUSED, _CANCEL_STOPPED, _CANCEL_DELETED):
                            self.cancel_reason = reason
                            if self.graph_task and not self.graph_task.done():
                                self.graph_task.cancel()
                            return
                        snap = await get_prediction_runtime_snapshot(self.prediction_id)
                        if snap is None or snap.status in (_PAUSED, "stopped"):
                            self.cancel_reason = (
                                _CANCEL_DELETED if snap is None else snap.status
                            )
                            if self.graph_task and not self.graph_task.done():
                                self.graph_task.cancel()
                            return
                    except Exception:
                        logger.exception("预测租约续期失败，停止当前 worker pred=%s", self.prediction_id)
                        self.cancel_reason = _CANCEL_LEASE_LOST
                        if self.graph_task and not self.graph_task.done():
                            self.graph_task.cancel()
                        return
                # Redis 活跃会话续期（带超时，防 Redis 慢时拖垮心跳；不可用时降级到进程内）
                try:
                    from db.redis_state import heartbeat_streaming
                    await asyncio.wait_for(
                        heartbeat_streaming(self.prediction_id), timeout=2,
                    )
                except Exception as exc:
                    # 铁律 #9：不静默吞（Redis 是可选依赖，降 debug 级别避免刷屏）
                    logger.debug(
                        "心跳续期 Redis 会话失败 pred=%s: %s", self.prediction_id, exc,
                    )
        except asyncio.CancelledError:
            pass

    def _cancel_heartbeat(self) -> None:
        if self.hb_task and not self.hb_task.done():
            self.hb_task.cancel()

    async def drain(self, skip_eid: int = 0) -> AsyncGenerator[dict, None]:
        """从队列消费事件直到终态。skip_eid <= 的事件跳过（已从 event_log replay）。

        终态时若队列已空则立即返回（避免阻塞），否则消费完剩余项 + None 哨兵。
        带超时轮询：图仍在运行且无事件时每 5s 醒一次检查 terminal，并补发一个 ping
        （双保险：心跳任务万一挂了，链路上也不会出现 >5s 的零字节空档）。
        """
        while True:
            if self.terminal and self.queue.empty():
                return
            try:
                item = await asyncio.wait_for(
                    self.queue.get(), timeout=_DRAIN_POLL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                if self.terminal:
                    return
                yield _ping_event()
                continue
            if item is None:
                return
            eid, ev = item
            # eid > 0 表示已持久化到 event_log；skip_eid 以下的事件 replay 已发过，跳过。
            # eid == 0 表示未持久化（ping / 写库失败），不可跳过，否则客户端会漏收。
            if eid > 0 and eid <= skip_eid:
                continue
            yield ev

    def stop(self) -> None:
        """取消后台图任务 + 心跳任务（管理 / 清理用；不留悬挂 task）。"""
        self.terminal = True
        self.cancel_reason = _CANCEL_STOPPED
        self._cancel_heartbeat()
        if self.graph_task and not self.graph_task.done():
            self.graph_task.cancel()

    def request_pause(self) -> bool:
        """暂停：只 cancel 图任务，**不立刻置 terminal**（与 stop 的关键差别）。

        置 terminal 会让 drain() 在队列空时立即返回 —— 那样 PAUSED 事件就赶不上这条 SSE 流，
        前端只看到连接无声无息地断掉（→ 判成 server_closed → 退避重连 → 谎报"连接已断开"）。
        这里让 _run_prediction_graph 的 CancelledError 分支去发 PAUSED + _finish_session()，
        terminal / 哨兵 / 心跳停止都在那里统一收口。
        返回 False = 没有活的图任务（已跑完或本 worker 没有它）。
        """
        self.cancel_reason = _CANCEL_PAUSED
        if self.graph_task and not self.graph_task.done():
            self.graph_task.cancel()
            return True
        return False


async def _emit(session: "PredictionSession", ev: dict) -> None:
    """持久化事件到 event_log + 放入队列（eid 用于重连去重，铁律 #4 async）。

    发给前端的 payload 会带上 event_log 的自增 eid（`id` 字段）——前端据此推进 after_event_id
    游标，重连时只拉增量，而不是把 4000+ 条 thinking 全量重放。
    （event_log 里存的 sse_string 不含 id；replay 时由 _decode_event_row 从行 id 补回，两条
    路径给到前端的 payload 因此完全一致。）
    """
    snapshot_id = session.prediction_id
    eid = 0
    try:
        eid = await append_event(
            conv_id=snapshot_id,
            event_type=ev.get("event", "unknown"),
            event_data=ev,
            sse_string=json.dumps(ev, ensure_ascii=False),
            message_id=snapshot_id,
        )
    except Exception as exc:
        # 铁律 #9：不吞，但不能让持久化失败阻塞图
        logger.warning(
            "event_log 写入失败 snap=%s ev=%s: %s",
            snapshot_id, ev.get("event"), exc,
        )
    out = dict(ev)
    if eid > 0:
        out["id"] = eid
    session.enqueue((eid, out))


async def _finish_session(session: "PredictionSession") -> None:
    """放 None 哨兵 + 标记终态 + 停心跳 + 清理注册表。"""
    session.terminal = True
    session._cancel_heartbeat()
    session.enqueue(None)
    if _SESSIONS.get(session.prediction_id) is session:
        _SESSIONS.pop(session.prediction_id, None)
    if session.lease_token:
        try:
            await prediction_runtime.release(session.prediction_id, session.lease_token)
        except Exception:
            logger.exception("释放预测租约失败 pred=%s", session.prediction_id)
    # 注销 Redis 活跃会话（可选，跨 worker）
    try:
        from db.redis_state import unregister_streaming
        await unregister_streaming(session.prediction_id)
    except Exception as exc:
        logger.debug("注销 Redis 活跃会话失败 pred=%s: %s", session.prediction_id, exc)


async def _run_prediction_graph(session: "PredictionSession", state: dict) -> None:
    """运行 predict_graph.astream_events，映射事件 → SSE dict → 队列 + event_log。

    事件映射（与旧 stream_predict 一致，节点不改）：
      on_custom_event llm_thinking → {event: THINKING/CONTENT, text: delta}
      on_custom_event prediction_event → {event: type, **payload}
      on_chain_end (news_search/compute/analyze/LangGraph) → 捕获 final_state

    图结束后：持久化 analysis + 落终态（done/failed，铁律 #6）+ 放 None 哨兵。
    """
    snapshot_id = session.prediction_id
    final_state: dict = {}
    t0 = time.perf_counter()

    try:
        async for ev_raw in predict_graph.astream_events(
            state, version="v2", config={"recursion_limit": 120},
        ):
            ev_type = ev_raw.get("event", "")
            ev_name = ev_raw.get("name", "")

            # ── emit_thinking 派发的自定义事件 → 映射 thinking/content ─────
            if ev_type == "on_custom_event" and ev_name == "llm_thinking":
                data = ev_raw.get("data") or {}
                phase = data.get("phase", "reasoning")
                delta = data.get("delta", "")
                if not delta:
                    continue
                # 铁律 #3：phase "reasoning" → THINKING，否则 CONTENT
                event_name = (
                    SSEEventType.THINKING.value if phase == "reasoning"
                    else SSEEventType.CONTENT.value
                )
                await _emit(session, {"event": event_name, "text": delta})
                continue

            # ── 节点派发的 prediction_event → 原样转发（type 已是枚举值）────
            if ev_type == "on_custom_event" and ev_name == "prediction_event":
                data = ev_raw.get("data") or {}
                ev_type_str = data.get("type")
                if not ev_type_str:
                    continue
                # 剥离 type，其余作为 payload
                payload = {k: v for k, v in data.items() if k != "type"}
                await _emit(session, {"event": ev_type_str, **payload})
                continue

            # ── 节点 / 图结束：捕获最终 state ─────────────────────────────
            # ★ "preflight" 必须在白名单里：预检失败时图直接短路到 END，若不抓它的 output，
            #   final_state 里就没有 error，图会被当成"成功收尾"落 done 而不是 failed。
            if ev_type == "on_chain_end" and (
                ev_name in _GRAPH_NODE_NAMES or ev_name == "LangGraph"
            ):
                output = (ev_raw.get("data") or {}).get("output") or {}
                if isinstance(output, dict):
                    final_state.update(output)

    except asyncio.CancelledError:
        if session.cancel_reason == _CANCEL_LEASE_LOST:
            # Another owner may have taken over. Never write a stopped/paused
            # status or terminal event on behalf of that new execution.
            await _finish_session(session)
            return
        # ── 删除（discard_session）：一条事件都不能再写 ──────────────────────
        # 否则 append_event 会在 DELETE FROM event_log 之后又插一行 STOPPED —— 那条预测
        # 已经不存在了，这行孤儿事件永远没人读、也永远删不掉（event_log 上零 FK）。
        # 同理不写 progress（快照行已被删，UPDATE 只会 rowcount=0 白跑一次）。
        if session.cancel_reason == _CANCEL_DELETED:
            logger.info("预测图取消（整单删除）snap=%s", snapshot_id)
            await _finish_session(session)
            return

        # ── 暂停（pause_prediction → session.request_pause()）───────────────
        # paused 不是终态：已完成的维度已增量落库（append_prediction_dimension），
        # resume 时只跑剩余维度。状态置 paused 由 pause_prediction 负责（已过 SM，铁律 #7）。
        # 这条 PAUSED 事件同样进 event_log → 暂停后刷新页面能 replay 出"已暂停"，
        # 而不是被 stream_predict 当成"worker 重启遗留"整单重跑。
        if session.cancel_reason == _CANCEL_PAUSED:
            logger.info("预测图取消（用户暂停）snap=%s", snapshot_id)
            await _emit(session, {
                "event": SSEEventType.PAUSED.value,
                "status": _PAUSED,
                "message": "预测已暂停（已完成的维度已保留，可继续）",
            })
            await _finish_session(session)
            return

        # 图被取消 = 用户点了停止（stop_prediction → session.stop() → graph_task.cancel()）。
        # 必须发一条 STOPPED 终态事件：否则 SSE 流只是"悄无声息地断掉"，前端会把它判成
        # server_closed（连接被服务端掐了）并开始指数退避重连 —— 用户明明自己点的停止，
        # 却看到"连接中断，正在重连 1/5"。STOPPED 也随 _emit 落进 event_log，
        # 停止后刷新页面时 replay 能立刻还原成"已停止"，而不是把任务当成遗留态整单重跑。
        # 状态置 stopped 由 stop_prediction() 负责（那里已过 PredictionSM，铁律 #6）。
        logger.info("预测图取消（用户停止）snap=%s", snapshot_id)
        await _finalize_progress(snapshot_id)
        await _emit(session, {"event": SSEEventType.STOPPED.value, "message": "预测已停止"})
        await _finish_session(session)
        return
    except Exception as exc:
        # 铁律 #9：异常不吞
        logger.warning(
            "预测图异常 snap=%s err=%s", snapshot_id, exc, exc_info=True,
        )
        try:
            if not await _transition_status(snapshot_id, "failed", error_message=str(exc)):
                await _finish_session(session)
                return
        except Exception as persist_exc:
            logger.warning("回写 failed 失败 snap=%s: %s", snapshot_id, persist_exc)
        await _finalize_progress(snapshot_id)
        await _emit(
            session,
            {"event": SSEEventType.ERROR.value, "message": f"预测流式失败：{exc}"},
        )
        await _finish_session(session)
        return

    # ── 图正常结束：持久化 analysis + 落终态（铁律 #6）─────────────────────
    analysis = final_state.get("analysis", "")
    risk_notes = final_state.get("risk_notes", []) or []
    thinking_segments = final_state.get("thinking_segments", []) or []
    err = final_state.get("error", "")
    err_kind = final_state.get("error_kind", "") or ""

    try:
        await update_prediction_analysis(
            snapshot_id, analysis, risk_notes, thinking_segments=thinking_segments,
        )
    except Exception as exc:
        logger.warning("回写 analysis 失败 snap=%s: %s", snapshot_id, exc)

    # 节点级 error（预检判死 / 资讯全挂 / forecast 失败）→ 落 failed（带错误类别）
    if err:
        try:
            if not await _transition_status(snapshot_id, "failed", error_message=err):
                await _finish_session(session)
                return
        except Exception as exc:
            logger.warning("回写 failed(error field) 失败 snap=%s: %s", snapshot_id, exc)
        # 清空进度 + 落错误类别（前端据 kind 区分「稍后重试」和「改代码」）
        await _finalize_progress(snapshot_id, error_kind=err_kind)
        logger.info(
            "预测图失败收尾 snap=%s kind=%s err=%s elapsed=%.0fms",
            snapshot_id, err_kind, err, (time.perf_counter() - t0) * 1000,
        )
        await _emit(session, {
            "event": SSEEventType.ERROR.value,
            "message": err,
            "error_kind": err_kind,
        })
        await _finish_session(session)
        return

    try:
        if not await _transition_status(snapshot_id, "done"):
            await _finish_session(session)
            return
    except Exception as exc:
        logger.warning("回写 done 失败 snap=%s: %s", snapshot_id, exc)

    # 终态清空进度：否则记录列表会一直挂着「正在搜索：地缘政治 (3/12)」的残留标签
    await _finalize_progress(snapshot_id)

    elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "预测图完成 snap=%s elapsed=%.0fms risk=%d thinking_segs=%d",
        snapshot_id, elapsed, len(risk_notes), len(thinking_segments),
    )

    done_ev = {
        "event": SSEEventType.DONE.value,
        "analysis": analysis,
        "risk_notes": risk_notes,
        "thinking_segments": thinking_segments,
        "news_digest": final_state.get("news_digest", []),
        "news_sentiment": final_state.get("news_sentiment", 0.0),
        # v2：数据型维度 + 事件综合分（forecast 里已含 model_params/formula_steps/forecast_candles）
        "filing_digest": final_state.get("filing_digest", []),
        "event_score": final_state.get("event_score", 0.0),
        "forecast": final_state.get("forecast", {}),
        "bars_summary": final_state.get("bars_summary", {}),
    }
    await _emit(session, done_ev)
    await _finish_session(session)


async def start_prediction_graph(
    snapshot_id: str, state: dict, *, lease_token: str | None = None,
) -> "PredictionSession | None":
    """Start one owner per prediction, even when requests hit different workers.

    Redis unavailability propagates to the caller: guessing ownership would
    duplicate market requests and model spend. An occupied lease is a normal
    attach-to-existing-execution outcome, represented by None.
    """
    existing = _SESSIONS.get(snapshot_id)
    if existing and not existing.terminal and lease_token is None:
        return existing
    token = lease_token or await prediction_runtime.acquire(snapshot_id)
    if token is None:
        return None
    session = None
    try:
        snap = await get_prediction_snapshot(snapshot_id)
        if snap is None or snap.status in (*_TERMINAL_STATUSES, _PAUSED):
            await prediction_runtime.release(snapshot_id, token)
            return None
        if state.get("resume") and snap.status != "queued":
            if not await _transition_status(snapshot_id, "queued"):
                await prediction_runtime.release(snapshot_id, token)
                return None
            state = {**state, "status": "queued"}
        session = PredictionSession(snapshot_id)
        session.lease_token = token
        _SESSIONS[snapshot_id] = session
        try:
            from db.redis_state import register_streaming
            await register_streaming(snapshot_id)
        except Exception:
            logger.exception("注册预测活跃标记失败 pred=%s", snapshot_id)
        session.start(_run_prediction_graph(session, state))
        return session
    except BaseException:
        if session is not None and _SESSIONS.get(snapshot_id) is session:
            _SESSIONS.pop(snapshot_id, None)
        await prediction_runtime.release(snapshot_id, token)
        raise


async def shutdown_prediction_sessions(timeout: float = 10.0) -> int:
    """Pause this worker's predictions before infrastructure is shut down.

    Set cancellation reasons before the first await so loop teardown cannot
    mistake a service restart for a user's permanent stop. Other workers' jobs
    are deliberately not queried or cancelled. If persistence/cleanup stalls,
    leave the ownership lease to expire instead of freeing a still-running job.
    """
    sessions = [session for session in list(_SESSIONS.values()) if not session.terminal]
    if not sessions:
        return 0
    for session in sessions:
        session.cancel_reason = _CANCEL_PAUSED
        session._cancel_heartbeat()

    async def pause_and_finish(session):
        changed = await _transition_status(
            session.prediction_id, _PAUSED, error_message="服务关闭，已保留完成结果，可点击继续",
        )
        if not changed:
            # Completion/stop may have won the DB race, or persistence failed.
            # Do not emit a misleading PAUSED event over an unchanged DB state.
            session.cancel_reason = _CANCEL_LEASE_LOST
        task = session.graph_task
        if task and not task.done():
            task.cancel()
        if task:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        if not session.terminal:
            await _finish_session(session)
        return bool(changed)

    def observe(task):
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logger.error("预测停机收尾异常", exc_info=(type(exc), exc, exc.__traceback__))

    jobs = [asyncio.create_task(pause_and_finish(session)) for session in sessions]
    for job in jobs:
        job.add_done_callback(observe)
    done, pending = await asyncio.wait(jobs, timeout=max(0.0, timeout))
    if pending:
        logger.warning("预测停机收尾超时，%d 个任务等待租约过期恢复", len(pending))
        for job in pending:
            job.cancel()
        # A slow DB pause might have prevented its helper from reaching cancel.
        for session in sessions:
            if session.graph_task and not session.graph_task.done():
                session.graph_task.cancel()
        await asyncio.sleep(0)
    return sum(bool(job.result()) for job in done if not job.cancelled() and job.exception() is None)


async def stop_prediction(prediction_id: str) -> dict:
    """停止预测：取消后台 graph_task → 取消 astream → 取消 LLM HTTP 流（不再发 token）。

    复用 chat 的 Redis stop 信号（chatflow:stop:{prediction_id}，跨 worker）+ 直接取消
    进程内 session.graph_task。asyncio 任务取消传播到节点内 llm.astream → httpx 请求中断
    → DeepSeek 端停止产 token（"不要再发 token" 的保证）。状态置 stopped（经 SM，铁律 #6）。
    """
    # 1. Redis stop 信号（跨 worker；同 worker 也供 _run_prediction_graph 主动 check_stop）
    try:
        from db.redis_state import publish_stop
        await publish_stop(prediction_id)
    except Exception as exc:
        logger.warning("stop_prediction publish_stop 失败 pred=%s: %s", prediction_id, exc)

    # 2. 取消进程内 session 的 graph_task（→ astream CancelledError → LLM HTTP 流中断）
    session = _SESSIONS.get(prediction_id)
    if session:
        try:
            session.stop()  # terminal=True + graph_task.cancel()
        except Exception as exc:
            logger.warning("stop_prediction session.stop 失败 pred=%s: %s", prediction_id, exc)
    else:
        try:
            await prediction_runtime.request_cancel(prediction_id, _CANCEL_STOPPED)
        except Exception:
            logger.exception("跨 worker 停止预测信号失败 pred=%s", prediction_id)

    # 3. 状态置 stopped（经 PredictionSM，铁律 #6；从任何运行态都可 stop，终态则 no-op）
    changed = await _transition_status(prediction_id, "stopped", error_message="用户停止")
    if not changed:
        current = await get_prediction_runtime_snapshot(prediction_id)
        return {"ok": False, "prediction_id": prediction_id,
                "status": current.status if current else "",
                "error": "任务状态已变化，未确认停止，请刷新后重试"}

    # 4. 清空阶段进度（否则记录列表会残留「正在搜索：地缘政治 (3/12)」标签）
    await _finalize_progress(prediction_id)

    return {"ok": True, "prediction_id": prediction_id, "status": "stopped"}


async def discard_session(prediction_id: str) -> bool:
    """删除前的收尾：取消在途图任务 + 心跳 + 清 Redis 会话标记（不写任何 DB 状态）。

    删除路径不能复用 stop_prediction：那会先把 status 写成 stopped，而这一行下一秒就要被
    DELETE 掉 —— 白写一次库，还会在 event_log 里留一条永远没人读的 STOPPED。
    返回 True = 确实取消了一个在跑的 session。
    """
    session = _SESSIONS.pop(prediction_id, None)
    if session is None:
        if await prediction_runtime.request_cancel(prediction_id, _CANCEL_DELETED):
            for _ in range(50):
                if not await prediction_runtime.owned(prediction_id):
                    return True
                await asyncio.sleep(0.1)
            raise RuntimeError("预测仍在停止中，请稍后重试删除")
        return False
    try:
        session.stop()
        # ★ 必须在 stop() 之后覆盖：stop() 内部会把 cancel_reason 设回 _CANCEL_STOPPED，
        #   而 CancelledError 分支要靠它判断"别再写库/发事件了，这行马上要被删"。
        session.cancel_reason = _CANCEL_DELETED
        if session.graph_task:
            try:
                await asyncio.wait_for(asyncio.shield(session.graph_task), timeout=5)
            except asyncio.CancelledError:
                if not session.graph_task.cancelled():
                    raise
    except Exception as exc:
        logger.warning("discard_session 取消失败 pred=%s: %s", prediction_id, exc)
        raise
    try:
        from db.redis_state import unregister_streaming
        await unregister_streaming(prediction_id)
    except Exception as exc:
        logger.debug("discard_session 注销 Redis 会话失败 pred=%s: %s", prediction_id, exc)
    return True


# ── 暂停 / 续跑（设计文档 §7.2）────────────────────────────────────────────────

def remaining_dimensions(snap) -> tuple[list[str], list[str]]:
    """(已完成维度, 剩余维度)。**"哪些维度算完成"的唯一判据**，router / 节点共用。

    完成 = 已落进 news_digest / filing_digest 且 failed 不为真。
    失败的维度算"未完成"（会重跑）—— 否则一次 Tavily 抖动就被永久固化成"该维无资讯"，
    用户点"继续"也永远补不回来。

    没有 checkpointer，做不到真冻结：cancel 会落在某个并发维度的 HTTP 请求中间，那一维的
    在途工作直接丢弃（损失上限 = 并发度 5 个维度）。这是设计文档 §7.2 已承认的代价。
    """
    criteria = snap.criteria or {}
    market = snap.market or criteria.get("market") or "cn_a"
    dims = resolve_dimensions(criteria.get("dimensions"), market)

    done: set[str] = set()
    for row in list(snap.news_digest or []) + list(snap.filing_digest or []):
        if not isinstance(row, dict) or row.get("failed"):
            continue
        key = str(row.get("dimension") or "")
        if key:
            done.add(key)

    completed = [d for d in dims if d in done]
    remaining = [d for d in dims if d not in done]
    return completed, remaining


async def pause_prediction(prediction_id: str) -> dict:
    """暂停预测：取消后台 graph_task + 经 PredictionSM 置 paused（**非终态**）。

    语义（用户拍板）：取消 + 保留已完成维度 + 可续跑剩余维度。不是"原地冻结"——
    全仓库没有 LangGraph checkpointer（设计文档 §7.2）。

    与 stop 的差别：
      - 不发 Redis stop 信号（那是给"永久停止"用的；resume 时还要再清一次，徒增竞态）
      - session.request_pause() 只 cancel 图任务、不立刻置 terminal → PAUSED 事件赶得上 SSE 流
      - 状态落 paused（可 resume），不是 stopped（终态）
      - 不清 progress：保留"停在第几维"，前端记录列表能显示"已暂停 · 5/16 维"
    """
    snap = await get_prediction_snapshot(prediction_id)
    if snap is None:
        return {"ok": False, "prediction_id": prediction_id, "status": "", "error": "预测任务不存在"}

    status = snap.status or "queued"
    if status in _TERMINAL_STATUSES:
        # 已 done/failed/stopped：暂停无意义，原样返回（不报错，前端按钮抖一下就好）
        return {"ok": False, "prediction_id": prediction_id, "status": status,
                "error": f"任务已处于终态（{status}），无法暂停"}
    if status == _PAUSED:
        return {"ok": True, "prediction_id": prediction_id, "status": _PAUSED}

    # Persist the pause before cancellation. A concurrent reader must never
    # observe a missing owner with a still-running DB status and restart it.
    if not await _transition_status(prediction_id, _PAUSED):
        current = await get_prediction_runtime_snapshot(prediction_id)
        return {"ok": False, "prediction_id": prediction_id,
                "status": current.status if current else "",
                "error": "任务状态已变化，未确认暂停，请刷新后重试"}
    session = _SESSIONS.get(prediction_id)
    cancelled = False
    if session:
        try:
            cancelled = session.request_pause()
        except Exception as exc:
            logger.warning("pause_prediction request_pause 失败 pred=%s: %s", prediction_id, exc)
    else:
        await prediction_runtime.request_cancel(prediction_id, _CANCEL_PAUSED)

    completed, remaining = remaining_dimensions(snap)
    logger.info(
        "预测已暂停 pred=%s from=%s cancelled=%s 已完成=%d 剩余=%d",
        prediction_id, status, cancelled, len(completed), len(remaining),
    )
    return {
        "ok": True,
        "prediction_id": prediction_id,
        "status": _PAUSED,
        "completed_dimensions": completed,
        "remaining_dimensions": remaining,
    }


async def resume_prediction(prediction_id: str) -> dict:
    """Resume under an ownership lease, then recheck DB state before mutation.

    Competing resume requests must not cancel each other's newly started graph
    or change state before knowing which worker owns this execution.
    """
    snap = await get_prediction_snapshot(prediction_id)
    if snap is None:
        return {"ok": False, "prediction_id": prediction_id, "status": "", "error": "预测任务不存在"}
    if snap.status != _PAUSED:
        return {"ok": False, "prediction_id": prediction_id, "status": snap.status,
                "error": f"任务当前状态为 {snap.status}，只有 paused 才能继续"}
    try:
        token = await prediction_runtime.acquire(prediction_id)
    except Exception:
        logger.exception("预测恢复获取租约失败 pred=%s", prediction_id)
        return {"ok": False, "prediction_id": prediction_id, "status": _PAUSED,
                "error": "预测服务暂时不可用，请稍后重试"}
    if token is None:
        return {"ok": False, "prediction_id": prediction_id, "status": _PAUSED,
                "error": "任务正在恢复或暂停收尾，请稍后重试"}

    handed_off = False
    try:
        snap = await get_prediction_snapshot(prediction_id)
        if snap is None or snap.status != _PAUSED:
            return {"ok": False, "prediction_id": prediction_id,
                    "status": snap.status if snap else "",
                    "error": "任务状态已变化，请刷新后重试"}
        completed, remaining = remaining_dimensions(snap)
        target = "queued" if not completed else "news_searching" if remaining else "inferring"
        if not await _transition_status(prediction_id, target, allow_resume=True):
            return {"ok": False, "prediction_id": prediction_id, "status": _PAUSED,
                    "error": "无法恢复任务状态，请稍后重试"}
        try:
            from db.redis_state import clear_stop
            await clear_stop(prediction_id)
        except Exception:
            logger.exception("清理旧预测停止信号失败 pred=%s", prediction_id)
        old = _SESSIONS.get(prediction_id)
        if old and old.graph_task and not old.graph_task.done():
            # Lease acquisition proves this old task no longer owns the job.
            old.cancel_reason = _CANCEL_LEASE_LOST
            old.graph_task.cancel()
            old._cancel_heartbeat()
        state = build_initial_state(
            snapshot_id=prediction_id, client_id=snap.client_id,
            user_id=snap.user_id or "", criteria=snap.criteria or {},
            status=target, resume=True,
        )
        started = await start_prediction_graph(prediction_id, state, lease_token=token)
        handed_off = started is not None
        if not handed_off:
            return {"ok": False, "prediction_id": prediction_id, "status": target,
                    "error": "任务状态已变化，请刷新后重试"}
        logger.info("预测已续跑 pred=%s 已完成=%d 剩余=%d", prediction_id, len(completed), len(remaining))
        return {"ok": True, "prediction_id": prediction_id, "status": target,
                "completed_dimensions": completed, "remaining_dimensions": remaining}
    finally:
        if not handed_off:
            await prediction_runtime.release(prediction_id, token)


# ── PRIMARY：SSE 流式 + RESUME（replay event_log → drain 队列 / 重跑）─────────

async def stream_predict(
    snapshot_id: str,
    after_event_id: int = 0,
    *, compact: bool = False,
) -> AsyncGenerator[dict, None]:
    """Read a private durable cursor per subscriber; reconnect never steals events.

    Local and remote-worker subscriptions share this path. Missing local state
    is not proof of an interrupted job: only acquiring the distributed lease
    permits recovery. Each database page is drained before checking completion.
    """
    async def read_events(after_id: int):
        if compact:
            return await get_compact_prediction_events(snapshot_id, after_id)
        return await get_events_since(snapshot_id, after_id=after_id, message_id=snapshot_id)

    last_id = after_event_id
    last_ping = time.monotonic()
    while True:
        events = await read_events(last_id)
        terminal = False
        for row in events:
            eid = row.get("id") or 0
            if eid <= last_id:
                continue
            last_id = eid
            kind = row.get("event_type")
            if kind == SSEEventType.PAUSED.value:
                # A previous run's pause is historical after an explicit resume.
                # Sending it again would make the browser close the new stream.
                paused_snap = await get_prediction_runtime_snapshot(snapshot_id)
                if paused_snap and paused_snap.status != _PAUSED:
                    continue
            yield _decode_event_row(row)
            terminal = kind in _STREAM_TERMINAL_EVENTS
        if terminal:
            return
        if events:
            # get_events_since is paginated. Do not stop after page one merely
            # because the snapshot has already reached its final status.
            continue

        snap = await get_prediction_runtime_snapshot(snapshot_id)
        if snap is None:
            yield {"event": SSEEventType.ERROR.value, "message": "预测任务不存在"}
            return
        status = snap.status or "queued"
        if status in (*_TERMINAL_STATUSES, _PAUSED):
            # Catch the race where the final event was persisted after this
            # iteration's read. If unavailable, the DB status still ends SSE.
            tail = await read_events(last_id)
            if tail:
                continue
            event_type = {
                "done": SSEEventType.DONE,
                "failed": SSEEventType.ERROR,
                "stopped": SSEEventType.STOPPED,
                _PAUSED: SSEEventType.PAUSED,
            }[status]
            yield {
                "event": event_type.value, "status": status,
                "message": getattr(snap, "error_message", "") or (
                    "预测已暂停（已完成的维度已保留，可继续）" if status == _PAUSED else "预测已结束"
                ),
            }
            return

        session = _SESSIONS.get(snapshot_id)
        if session is None or session.terminal:
            try:
                if not await prediction_runtime.owned(snapshot_id):
                    state = build_initial_state(
                        snapshot_id=snapshot_id,
                        client_id=snap.client_id,
                        user_id=snap.user_id or "",
                        criteria=snap.criteria or {},
                        status=status,
                        resume=True,
                    )
                    session = await start_prediction_graph(snapshot_id, state)
            except Exception:
                logger.exception("预测恢复暂不可用 pred=%s", snapshot_id)
                yield {"event": SSEEventType.ERROR.value,
                       "message": "预测服务暂时无法恢复，请稍后重试；已完成的结果已保存"}
                return
        if time.monotonic() - last_ping >= HEARTBEAT_INTERVAL_SECONDS:
            yield _ping_event()
            last_ping = time.monotonic()
        if session and not session.terminal:
            session.changed.clear()
            try:
                await asyncio.wait_for(session.changed.wait(), timeout=0.5)
                # Coalesce token bursts into one DB read instead of polling
                # once per token per open browser tab.
                await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(0.5)


def _decode_event_row(evt: dict) -> dict:
    """从 event_log 行还原 SSE dict（优先 sse_string JSON，回退 event_data）。

    补回行的自增 id：sse_string 是入库前序列化的、不含 id，而前端要靠 `id` 推进 after_event_id
    游标。replay 和 live（_emit）两条路径因此给到前端的 payload 结构完全一致。
    """
    ev: dict = {}
    sse_str = evt.get("sse_string")
    if sse_str:
        try:
            ev = json.loads(sse_str)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("event_log 行 sse_string 解析失败 id=%s: %s", evt.get("id"), exc)
    if not isinstance(ev, dict) or not ev:
        ev = dict(evt.get("event_data") or {})
    eid = evt.get("id") or 0
    if eid:
        ev["id"] = eid
    return ev
