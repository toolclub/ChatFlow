"""停止预测（stop_prediction + PredictionSM.stop）—— 单元测试。

验证「停止时不再发 token」的关键链路：
  - PredictionSM.stop：queued/news_searching/inferring → stopped（终态）；done/failed → no-op（不崩）
  - stop_prediction：publish_stop(Redis 跨 worker 信号) + session.stop()（取消 graph_task
    → astream CancelledError → LLM HTTP 流中断，不再产 token）+ _transition_status("stopped")
"""
from __future__ import annotations

import asyncio

import pytest

import graph.prediction_agent as pa
from fsm.prediction import PredictionStatus, PredictionSM


@pytest.fixture(autouse=True)
def _no_remote_owner(monkeypatch):
    async def _request_cancel(*args):
        return False
    monkeypatch.setattr(pa.prediction_runtime, "request_cancel", _request_cancel)


class _FakeSession:
    """替代 PredictionSession，记录 stop() 是否被调用（即 graph_task 是否被取消）。"""

    def __init__(self) -> None:
        self.stopped = False
        self.terminal = False
        self.graph_task = None

    def stop(self) -> None:
        self.stopped = True
        self.terminal = True


# ── PredictionSM.stop 状态转换 ───────────────────────────────────────────────

@pytest.mark.unit
def test_sm_stop_from_running_states():
    """任一运行态 → stopped（终态）。"""
    for src in ("queued", "news_searching", "inferring"):
        sm = PredictionSM.from_db_status(src)
        new = sm.send_event("stopped")
        assert new == "stopped", f"{src} → stopped 失败：得到 {new}"


@pytest.mark.unit
def test_sm_stop_from_terminal_is_noop():
    """终态（done/failed）stop：无转换，保持原状，不崩（铁律 #9 记 warning 不抛）。"""
    for src in ("done", "failed"):
        sm = PredictionSM.from_db_status(src)
        new = sm.send_event("stopped")
        assert new == src, f"{src} 终态 stop 应 no-op，却变成 {new}"


@pytest.mark.unit
def test_prediction_status_has_stopped():
    """枚举含 STOPPED='stopped'（终态；前端按此着色）。"""
    assert PredictionStatus.STOPPED.value == "stopped"
    assert "stopped" in {s.value for s in PredictionStatus}


# ── stop_prediction：取消 graph_task + publish_stop + 置 stopped ─────────────

@pytest.mark.unit
def test_stop_prediction_cancels_session_and_signals(monkeypatch):
    """核心：stop_prediction 必须取消 session.graph_task（→ 不再发 token）+ 发 Redis stop 信号。"""
    fake_session = _FakeSession()
    monkeypatch.setitem(pa._SESSIONS, "pred_stop_1", fake_session)

    publish_calls: list[str] = []

    async def _fake_publish(pid: str) -> None:
        publish_calls.append(pid)

    monkeypatch.setattr("db.redis_state.publish_stop", _fake_publish, raising=False)

    # _transition_status 会读 get_prediction_snapshot + 写 update_prediction_status —— mock 掉避免触 DB
    async def _fake_get_snap(sid: str):
        class _S:
            status = "inferring"
        return _S()

    written: list[tuple] = []

    async def _fake_update_status(sid: str, status: str, error_message: str | None = None) -> None:
        written.append((sid, status, error_message))

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _fake_update_status)

    res = asyncio.run(pa.stop_prediction("pred_stop_1"))

    assert res == {"ok": True, "prediction_id": "pred_stop_1", "status": "stopped"}
    # 关键断言：graph_task 被取消 → astream 中断 → LLM 不再发 token
    assert fake_session.stopped is True, "session.stop() 未调用 → graph_task 未取消 → LLM 仍在产 token"
    assert publish_calls == ["pred_stop_1"], "publish_stop 未发 Redis stop 信号（跨 worker 失效）"
    # 状态写库：经 SM 从 inferring → stopped
    assert any(sid == "pred_stop_1" and status == "stopped" for sid, status, _ in written), \
        f"未写 stopped 状态：{written}"


@pytest.mark.unit
def test_stop_prediction_no_session_still_signals_and_marks(monkeypatch):
    """无进程内 session（跨 worker / 已结束）：仍发 Redis 信号 + 置 stopped，不崩。"""
    pa._SESSIONS.pop("pred_stop_2", None)  # 确保无 session

    publish_calls: list[str] = []

    async def _fake_publish(pid: str) -> None:
        publish_calls.append(pid)

    monkeypatch.setattr("db.redis_state.publish_stop", _fake_publish, raising=False)

    async def _fake_get_snap(sid: str):
        class _S:
            status = "news_searching"
        return _S()

    async def _fake_update_status(sid: str, status: str, error_message: str | None = None) -> None:
        return None

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _fake_update_status)

    res = asyncio.run(pa.stop_prediction("pred_stop_2"))
    assert res["status"] == "stopped"
    assert publish_calls == ["pred_stop_2"]  # Redis 信号照发（跨 worker 通知）
