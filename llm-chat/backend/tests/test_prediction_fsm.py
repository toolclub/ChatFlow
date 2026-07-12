"""股票预测状态机 (fsm/prediction.py) —— 单元测试

覆盖 T-PRED-* 用例（见 test_case.md「十二、预测状态机」）：
  1. PredictionSM 全路径: queued → news_searching → inferring → done
  2. 任一运行态 → failed（queued/news_searching/inferring 各自 fail）
  3. 非法转换（跳步 / 终态外转 / 未知目标）返回当前值不变 + 记 warning，不抛
  4. from_db_status 恢复：有效状态续命，garbage 降级到 queued
  5. PredictionStatus 枚举恰好 5 个字符串值，(str, Enum) 兼容
  6. cleanup_stale_prediction_sessions 把卡住的运行态批量置 failed（mock AsyncSessionLocal）

隔离策略（对齐 test_quant_analyze.py / test_quant_screen.py）：
  - fsm/prediction 仅依赖 python-statemachine，运行时（Docker 内）才 import；
    顶层只 import pytest，故 collection 不依赖任何重依赖。
  - test_06 在方法内 import db.prediction_store，并 monkeypatch 其
    AsyncSessionLocal（假异步会话）与 update（假语句工厂），断言 status=failed
    落库逻辑，不触碰真实 DB/Redis/SQLAlchemy 执行。
运行：pytest tests/test_prediction_fsm.py -v -m unit
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest


# ═══════════════════════════════════════════════════════════════
# T-PRED-01: 全路径合法转换
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionSMTransitions:
    def _make(self):
        from fsm.prediction import PredictionSM
        return PredictionSM()

    def test_full_path_queued_to_done(self):
        """queued → news_searching → inferring → done，每步 current_value 正确。"""
        sm = self._make()
        assert sm.current_value == "queued"

        assert sm.send_event("news_searching") == "news_searching"
        assert sm.current_value == "news_searching"

        assert sm.send_event("inferring") == "inferring"
        assert sm.current_value == "inferring"

        assert sm.send_event("done") == "done"
        assert sm.current_value == "done"

    def test_send_event_returns_new_value(self):
        """send_event 返回转换后的状态字符串（用于编排层决策）。"""
        sm = self._make()
        assert sm.send_event("news_searching") == "news_searching"
        assert sm.send_event("inferring") == "inferring"
        assert sm.send_event("done") == "done"


# ═══════════════════════════════════════════════════════════════
# T-PRED-02: 任一运行态 → failed
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionSMFail:
    def test_queued_to_failed(self):
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        assert sm.send_event("failed") == "failed"
        assert sm.current_value == "failed"

    def test_news_searching_to_failed(self):
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        sm.send_event("news_searching")
        assert sm.send_event("failed") == "failed"
        assert sm.current_value == "failed"

    def test_inferring_to_failed(self):
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        sm.send_event("news_searching")
        sm.send_event("inferring")
        assert sm.send_event("failed") == "failed"
        assert sm.current_value == "failed"


# ═══════════════════════════════════════════════════════════════
# T-PRED-03: 非法转换（不抛、状态不变、记 warning）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionSMInvalid:
    def test_queued_to_done_rejected(self):
        """跳步 queued→done：返回当前值不变，不抛。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        result = sm.send_event("done")
        assert result == "queued"
        assert sm.current_value == "queued"

    def test_queued_to_inferring_rejected(self):
        """跳步 queued→inferring（漏 news_searching）：状态不变。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        result = sm.send_event("inferring")
        assert result == "queued"
        assert sm.current_value == "queued"

    def test_done_final_cannot_transition(self):
        """终态 done 不能再转（含 failed）：返回 done 不变。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        sm.send_event("news_searching")
        sm.send_event("inferring")
        sm.send_event("done")
        result = sm.send_event("failed")
        assert result == "done"
        assert sm.current_value == "done"

    def test_failed_final_cannot_transition(self):
        """终态 failed 不能回到任何状态。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        sm.send_event("failed")
        for target in ("news_searching", "inferring", "done", "failed"):
            assert sm.send_event(target) == "failed"

    def test_unknown_target_rejected(self):
        """未知目标状态：状态不变。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        result = sm.send_event("garbage")
        assert result == "queued"
        assert sm.current_value == "queued"

    def test_invalid_logs_warning(self, caplog):
        """非法转换必须记 warning（铁律 #9：异常不吞）。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        with caplog.at_level(logging.WARNING, logger="statemachine.prediction"):
            sm.send_event("done")          # 跳步，走 exception 分支
            sm.send_event("nonexistent")   # 未知目标，走无效分支
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "非法转换应记 warning"

    def test_invalid_does_not_raise(self):
        """send_event 对任何输入都不抛异常（编排层不崩）。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        for target in ("done", "inferring", "failed", "???", ""):
            # 不抛即通过
            sm.send_event(target)


# ═══════════════════════════════════════════════════════════════
# T-PRED-06: paused（v2）—— 暂停 = 取消 + 保留已完成维度 + 续跑剩余
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionSMPause:
    def test_any_running_state_can_pause(self):
        """任一运行态 → paused。"""
        from fsm.prediction import PredictionSM
        for src in ("queued", "news_searching", "inferring"):
            sm = PredictionSM.from_db_status(src)
            assert sm.send_event("paused") == "paused", src

    def test_paused_is_not_final(self):
        """paused 不是终态（stopped 才是）—— 否则 resume 永远转不出去。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM.from_db_status("paused")
        assert not sm.configuration[0].final

    def test_resume_lands_on_requested_state(self):
        """★ 回归：paused → 目标态必须落在**请求的**那个状态。

        resume 若写成单个多目标事件（paused.to(queued) | paused.to(news_searching) | ...），
        python-statemachine 会静默取第一个可行目标 → send_event("inferring") 落到 queued，
        续跑时把已经跑完的资讯维度又重跑一遍。故三个 resume 必须是独立单目标事件。
        """
        from fsm.prediction import PredictionSM
        for target in ("queued", "news_searching", "inferring"):
            sm = PredictionSM.from_db_status("paused")
            assert sm.send_event(target) == target, target

    def test_paused_can_stop_and_fail(self):
        """paused 可直接停止 / 失败（终态）。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM.from_db_status("paused")
        assert sm.send_event("stopped") == "stopped"
        sm = PredictionSM.from_db_status("paused")
        assert sm.send_event("failed") == "failed"

    def test_terminal_cannot_pause(self):
        """终态不能暂停：状态不变。"""
        from fsm.prediction import PredictionSM
        for src in ("done", "failed", "stopped"):
            sm = PredictionSM.from_db_status(src)
            assert sm.send_event("paused") == src, src

    def test_pause_resume_roundtrip(self):
        """暂停后续跑，仍能正常走到 done。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM()
        sm.send_event("news_searching")
        assert sm.send_event("paused") == "paused"
        assert sm.send_event("news_searching") == "news_searching"   # 续跑剩余维度
        assert sm.send_event("inferring") == "inferring"
        assert sm.send_event("done") == "done"


# ═══════════════════════════════════════════════════════════════
# T-PRED-04: from_db_status 恢复
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionSMRestore:
    def test_resume_inferring(self):
        """from_db_status('inferring') 续命：可继续 → done。"""
        from fsm.prediction import PredictionSM
        sm = PredictionSM.from_db_status("inferring")
        assert sm.current_value == "inferring"
        assert sm.send_event("done") == "done"

    def test_resume_each_status(self):
        from fsm.prediction import PredictionSM
        for s in ("queued", "news_searching", "inferring", "done", "failed"):
            sm = PredictionSM.from_db_status(s)
            assert sm.current_value == s

    def test_garbage_falls_back_to_queued(self):
        from fsm.prediction import PredictionSM
        sm = PredictionSM.from_db_status("garbage")
        assert sm.current_value == "queued"

    def test_empty_falls_back_to_queued(self):
        from fsm.prediction import PredictionSM
        sm = PredictionSM.from_db_status("")
        assert sm.current_value == "queued"


# ═══════════════════════════════════════════════════════════════
# T-PRED-05: PredictionStatus 枚举
# ═══════════════════════════════════════════════════════════════
@pytest.mark.unit
class TestPredictionStatusEnum:
    def test_exactly_seven_values(self):
        """枚举恰好 7 个字符串值（v2 新增 paused=暂停，**非终态**，可 resume 续跑）。"""
        from fsm.prediction import PredictionStatus
        values = {s.value for s in PredictionStatus}
        assert values == {
            "queued", "news_searching", "inferring", "paused", "done", "failed", "stopped",
        }
        assert len(values) == 7

    def test_str_enum_compare(self):
        """(str, Enum) 使枚举值可直接与字符串比较。"""
        from fsm.prediction import PredictionStatus
        assert PredictionStatus.QUEUED == "queued"
        assert PredictionStatus.NEWS_SEARCHING == "news_searching"
        assert PredictionStatus.INFERRING == "inferring"
        assert PredictionStatus.DONE == "done"
        assert PredictionStatus.FAILED == "failed"

    def test_construct_from_string(self):
        from fsm.prediction import PredictionStatus
        assert PredictionStatus("queued") == PredictionStatus.QUEUED
        assert PredictionStatus("failed") == PredictionStatus.FAILED

    def test_json_serialization(self):
        """继承 (str, Enum) 使 json.dumps 无需 default 转换。"""
        from fsm.prediction import PredictionStatus
        out = json.dumps({"s": PredictionStatus.NEWS_SEARCHING})
        assert out == '{"s": "news_searching"}'


# ═══════════════════════════════════════════════════════════════
# T-PRED-06: cleanup_stale_prediction_sessions（mock AsyncSessionLocal）
# ═══════════════════════════════════════════════════════════════
class _FakeUpdateStmt:
    """假 update() 语句：链式 .where().values()，捕获 values 落库内容。"""

    def __init__(self, model=None):
        self.model = model
        self.where_calls: list = []
        self.values_map: dict = {}

    def where(self, *conds):
        self.where_calls.append(conds)
        return self

    def values(self, **kw):
        self.values_map.update(kw)
        return self


class _FakeResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    """假异步会话：记录 execute/commit，返回可配置 rowcount。"""

    def __init__(self, rowcount: int = 3):
        self._rowcount = rowcount
        self.executed: list = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._rowcount)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSessionLocal:
    """替代 db.database.AsyncSessionLocal：返回共享的 _FakeSession。"""

    def __init__(self, session: _FakeSession):
        self._session = session

    def __call__(self):
        return self._session


@pytest.mark.unit
def test_cleanup_marks_running_statuses_to_failed(monkeypatch):
    """无 timeout 调用：把卡在 queued/news_searching/inferring 的行置 failed，
    返回受影响行数，commit 已执行，error_message 为「服务重启」原因。"""
    import db.prediction_store as ps

    session = _FakeSession(rowcount=7)
    monkeypatch.setattr(ps, "AsyncSessionLocal", _FakeSessionLocal(session))
    monkeypatch.setattr(ps, "update", lambda model: _FakeUpdateStmt(model))

    count = asyncio.run(ps.cleanup_stale_prediction_sessions())

    # 返回受影响行数
    assert count == 7
    # session.execute 被调用且 commit 被执行（持久化铁律 #4）
    assert len(session.executed) == 1
    assert session.committed is True
    # 落库 status=failed + 非空 error_message（铁律 #2：结构化字段独立）
    stmt = session.executed[0]
    assert stmt.values_map.get("status") == "failed"
    assert stmt.values_map.get("error_message")  # 非空
    assert "中断" in stmt.values_map["error_message"]


@pytest.mark.unit
def test_cleanup_with_timeout_sets_timeout_reason(monkeypatch):
    """带 timeout_minutes：error_message 含超时分钟数，新增 created_at where 条件。"""
    import db.prediction_store as ps

    session = _FakeSession(rowcount=2)
    monkeypatch.setattr(ps, "AsyncSessionLocal", _FakeSessionLocal(session))
    monkeypatch.setattr(ps, "update", lambda model: _FakeUpdateStmt(model))

    count = asyncio.run(ps.cleanup_stale_prediction_sessions(timeout_minutes=5))

    assert count == 2
    stmt = session.executed[0]
    assert stmt.values_map.get("status") == "failed"
    assert "5" in stmt.values_map["error_message"]
    # timeout 路径多一条 where（created_at < cutoff）
    assert len(stmt.where_calls) >= 2


@pytest.mark.unit
def test_cleanup_zero_rows_returns_zero(monkeypatch):
    """没有卡住的任务：rowcount=0，仍正常 commit。"""
    import db.prediction_store as ps

    session = _FakeSession(rowcount=0)
    monkeypatch.setattr(ps, "AsyncSessionLocal", _FakeSessionLocal(session))
    monkeypatch.setattr(ps, "update", lambda model: _FakeUpdateStmt(model))

    count = asyncio.run(ps.cleanup_stale_prediction_sessions())

    assert count == 0
    assert session.committed is True
    # 即便 0 行，仍把 status=failed 写入语句
    assert session.executed[0].values_map.get("status") == "failed"
