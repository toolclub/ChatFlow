"""v2 持久化（db/prediction_store.py）+ 增量落库 + 删除 + 刷新重建 —— 纯 mock，无 Postgres。

AsyncSessionLocal 被换成假 session：记录每条 SQL 与参数。断言的是**契约**
（写哪一列、去重键是什么、删不删 event_log），不是 SQL 字符串长相。

  T-PRED-STORE-01  append_prediction_dimension：search→news_digest / data→filing_digest，
                   去重键恒为 dimension（续跑重写同一维不会长出重复卡片）
  T-PRED-STORE-02  formula_step 去重键 = stage；forecast_candle = date；tool_trace 不去重
  T-PRED-STORE-03  列名白名单：非法列名直接拒绝，一条 SQL 都不发（列名会拼进 SQL）
  T-PRED-STORE-04  快照不存在（rowcount=0）→ False，不抛
  T-PRED-STORE-05  写库炸了 → False + 不抛（铁律 #9：增量落库失败不能阻塞图执行）
  T-PRED-STORE-06  ★ delete_prediction 显式 DELETE event_log（零 FK，级联是 no-op）
  T-PRED-STORE-07  delete_prediction：快照不存在 → False
  T-PRED-STORE-08  ★ 刷新重建：8 个 v2 列全部能从快照读回（前端不靠事件重放恢复）
  T-PRED-STORE-09  ★ 增量落库真的发生在「每维跑完时」，不是等全部维度跑完才一次性写
                   （v1 就是一次性写 → 一 cancel 库里一条都没有，「暂停续跑」无从谈起）

★ T-PRED-STORE-06 的由来：event_log 上零 FK 约束（migrate.py 的 CREATE TABLE IF NOT EXISTS
  因 create_all 先执行而永久 no-op，声明的 ON DELETE CASCADE 从未落地）。指望级联 =
  每删一条预测泄漏几百行孤儿事件。
"""
from __future__ import annotations

import asyncio
import types

import pytest

import db.prediction_store as store
import graph.nodes.prediction.news_search_node as news_mod
import graph.prediction_agent as pa

pytestmark = pytest.mark.unit


# ── 假 DB session ────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    """记录 execute(sql, params)；rowcount 由 fixture 指定；可指定抛错。"""

    def __init__(self, log: list, rowcounts: list[int], boom: Exception | None):
        self._log = log
        self._rowcounts = rowcounts
        self._boom = boom
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, params=None):
        if self._boom is not None:
            raise self._boom
        self._log.append({"sql": str(sql), "params": dict(params or {})})
        rc = self._rowcounts.pop(0) if self._rowcounts else 1
        return _Result(rc)

    async def commit(self):
        self.committed = True


@pytest.fixture
def db(monkeypatch):
    """把 AsyncSessionLocal 换成假 session 工厂。"""
    state = types.SimpleNamespace(log=[], rowcounts=[], boom=None)

    def _factory():
        return _FakeSession(state.log, state.rowcounts, state.boom)

    monkeypatch.setattr(store, "AsyncSessionLocal", _factory)
    return state


def _sql_of(db, i: int = 0) -> str:
    return db.log[i]["sql"]


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-STORE-01 / 02：增量落库写对列 + 去重键
# ══════════════════════════════════════════════════════════════════════════════

def test_dimension_routes_to_column_by_kind(db):
    """T-PRED-STORE-01：kind 决定落哪一列 —— 走错列前端就永远读不到那批卡片。"""
    ok = asyncio.run(store.append_prediction_dimension(
        "pred_1", {"dimension": "macro_policy", "sentiment": 0.3}, kind="search",
    ))
    assert ok is True
    assert "news_digest" in _sql_of(db, 0)
    assert "filing_digest" not in _sql_of(db, 0)
    assert db.log[0]["params"]["dedupe_key"] == "dimension"

    db.log.clear()
    ok = asyncio.run(store.append_prediction_dimension(
        "pred_1", {"dimension": "negative_event", "kind": "data", "score": -0.5}, kind="data",
    ))
    assert ok is True
    assert "filing_digest" in _sql_of(db, 0)
    assert db.log[0]["params"]["dedupe_key"] == "dimension"


def test_dimension_dedupe_key_makes_rewrite_idempotent(db):
    """同一维度重复写（续跑/重试）必须覆盖而不是追加 —— SQL 里要有按 dedupe_key 剔旧的子查询。"""
    asyncio.run(store.append_prediction_dimension(
        "pred_1", {"dimension": "negative_event"}, kind="data",
    ))
    sql = _sql_of(db, 0)
    assert "jsonb_array_elements" in sql and "IS DISTINCT FROM" in sql, (
        "缺少按 dedupe_key 剔除旧元素的子查询 → 续跑会长出重复维度卡片"
    )


def test_append_dedupe_keys_per_column(db):
    """T-PRED-STORE-02：公式步骤按 stage 去重、蜡烛按 date 去重、工具轨迹不去重。"""
    asyncio.run(store.append_prediction_formula_step("p", {"key": "garch", "stage": "garch"}))
    assert "formula_steps" in _sql_of(db, 0)
    assert db.log[0]["params"]["dedupe_key"] == "stage", (
        "公式步骤去重键必须是 stage —— engine 的 _step_payload 就是按这个补字段的"
    )

    db.log.clear()
    asyncio.run(store.append_prediction_forecast_candle("p", {"day": 1, "date": "2026-07-13"}))
    assert "forecast_candles" in _sql_of(db, 0)
    assert db.log[0]["params"]["dedupe_key"] == "date", (
        "蜡烛去重键必须是 date，否则刷新重放会画出 14 根蜡烛"
    )

    db.log.clear()
    asyncio.run(store.append_prediction_tool_trace("p", {"tool": "fetch_reductions"}))
    assert "tool_traces" in _sql_of(db, 0)
    assert "dedupe_key" not in db.log[0]["params"], (
        "工具轨迹不该去重：同一工具会被多个维度各调一次，每次都是独立轨迹"
    )


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-STORE-03 / 04 / 05：护栏
# ══════════════════════════════════════════════════════════════════════════════

def test_illegal_column_is_rejected_without_sql(db):
    """T-PRED-STORE-03：列名会拼进 SQL → 只能走白名单。非法列名一条 SQL 都不许发。"""
    ok = asyncio.run(store._append_jsonb_item("p", "news_digest; DROP TABLE users--", {"x": 1}))

    assert ok is False
    assert db.log == [], "非法列名仍然发了 SQL —— 这是注入面"


def test_missing_snapshot_returns_false(db):
    """T-PRED-STORE-04：快照不存在（rowcount=0）→ False，不抛。"""
    db.rowcounts = [0]
    ok = asyncio.run(store.append_prediction_formula_step("ghost", {"stage": "arima"}))
    assert ok is False


def test_write_failure_degrades_not_raises(db):
    """T-PRED-STORE-05：写库炸了只返回 False（铁律 #9：warning 不吞、但不能阻塞图执行）。"""
    db.boom = RuntimeError("connection pool exhausted")

    ok = asyncio.run(store.append_prediction_forecast_candle("p", {"date": "2026-07-13"}))

    assert ok is False, "写失败必须返回 False 让调用方知情"
    # 没有异常逃出来 —— 一次落库失败不能把整张预测图带崩


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-STORE-06 / 07：删除
# ══════════════════════════════════════════════════════════════════════════════

def test_delete_prediction_explicitly_deletes_event_log(db):
    """★ T-PRED-STORE-06：event_log 零 FK，级联是 no-op → 必须显式 DELETE。"""
    db.rowcounts = [42, 1]      # event_log 42 行 + 快照 1 行

    ok = asyncio.run(store.delete_prediction("pred_del"))

    assert ok is True
    sqls = [r["sql"] for r in db.log]
    assert len(sqls) == 2, f"应发两条 DELETE，实得 {len(sqls)}"

    ev_sql, snap_sql = sqls[0], sqls[1]
    assert "DELETE FROM event_log" in ev_sql and "conv_id" in ev_sql, (
        "没有显式删 event_log —— 每删一条预测就泄漏几百行孤儿事件"
    )
    assert "DELETE FROM prediction_snapshots" in snap_sql
    assert all(r["params"]["pid"] == "pred_del" for r in db.log)


def test_delete_missing_snapshot_returns_false(db):
    """T-PRED-STORE-07：快照不存在 → False（router 据此返回 404）。"""
    db.rowcounts = [0, 0]
    assert asyncio.run(store.delete_prediction("nope")) is False


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-STORE-08：★ 刷新重建 —— 8 个 v2 列全部读得回来
# ══════════════════════════════════════════════════════════════════════════════

def test_v2_fields_roundtrip_for_refresh_rebuild():
    """★ 前端 loadSnapshot() 靠这些 DB 列重建整个 v2 界面（不靠 SSE 重放）。

    少一列 → 硬刷新后对应区块直接空白（公式卡片 / 预测蜡烛 / 公告打点 / 工具日志）。
    """
    written = {
        "filing_digest": [{"dimension": "negative_event", "kind": "data",
                           "items": [{"title": "立案调查", "confidence": "llm_inferred"}],
                           "score": -0.5, "source_note": "基于公告标题与公告类型分类…"}],
        "model_params": {"arima_order": [1, 1, 0], "garch_alpha": 0.08, "filing_score": -0.31},
        "formula_steps": [{"key": "garch", "stage": "garch", "result": 0.3}],
        "forecast_candles": [{"day": 1, "date": "2026-07-13", "o": 10, "h": 11, "l": 9, "c": 10.5}],
        "event_markers": [{"date": "2026-07-01", "event_type": "negative", "score": -0.9}],
        "tool_traces": [{"phase": "end", "tool": "fetch_reductions", "rows": 12, "ok": True}],
        "event_score": -0.31,
        "historical_bars": {"dates": ["2026-07-01"], "values": [[10, 10.5, 9, 11]],
                             "ma5": [None], "ma10": [None], "ma20": [None], "volumes": [[0, 100, 1]]},
    }
    snap = types.SimpleNamespace(**written)

    out = store.prediction_v2_fields(snap)

    assert set(out) == set(written), f"v2 列缺失/多余: {set(out) ^ set(written)}"
    for key, val in written.items():
        assert out[key] == val, f"{key} 读回的值与写入的不一致"


def test_v2_fields_empty_snapshot_gives_empty_containers():
    """全新快照（列还是 NULL）→ 空容器而不是 None（前端 v-for 直接炸）。"""
    snap = types.SimpleNamespace(
        filing_digest=None, model_params=None, formula_steps=None,
        forecast_candles=None, event_markers=None, tool_traces=None, event_score=None,
        historical_bars=None,
    )

    out = store.prediction_v2_fields(snap)

    assert out["filing_digest"] == [] and out["formula_steps"] == []
    assert out["forecast_candles"] == [] and out["event_markers"] == [] and out["tool_traces"] == []
    assert out["model_params"] == {}
    assert out["event_score"] is None, "event_score 没算出来时是 None，不能谎报 0（= 无风险）"
    assert out["historical_bars"] == {}, "旧记录/运行中未落库 → 空对象，前端据此回退现拉"


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-STORE-09：★ 增量落库 —— 每维跑完就写，不是等全部跑完
# ══════════════════════════════════════════════════════════════════════════════

def test_dimension_persisted_as_each_one_finishes(monkeypatch):
    """★ 每个维度一跑完就 append 一次（v1 是全跑完一次性写 → cancel 后库里一条都没有）。

    这条是「暂停 = 取消 + 保留已完成维度 + 续跑剩余」的唯一前提：
    没有增量落库，暂停后 remaining_dimensions() 会认为一个维度都没完成，全部重跑。
    """
    appended: list[tuple[str, str]] = []      # (dimension, kind)
    finished_before_gather_returns: list[str] = []

    async def _fake_append_dim(sid, result, kind="search"):
        appended.append((result["dimension"], kind))
        return True

    async def _fake_gather(symbol, market, pid, dims=None, **kw):
        on_dim = kw.get("on_dimension")
        # 模拟 gather 内部：每维跑完立刻回调（真实实现就是这个语义）
        for d in [
            {"dimension": "macro_policy", "sentiment": 0.2, "failed": False},
            {"dimension": "negative_event", "kind": "data", "score": -0.5,
             "items": [], "failed": False},
        ]:
            await on_dim(d)
            finished_before_gather_returns.append(d["dimension"])
        return {
            "news_digest": [{"dimension": "macro_policy", "sentiment": 0.2, "failed": False}],
            "news_sentiment": 0.2,
            "filing_digest": [{"dimension": "negative_event", "kind": "data",
                               "score": -0.5, "items": [], "failed": False}],
        }

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(news_mod, "gather_all_dimensions", _fake_gather)
    monkeypatch.setattr(news_mod, "append_prediction_dimension", _fake_append_dim)
    for fn in ("update_prediction_status", "update_prediction_progress",
               "update_prediction_news", "update_prediction_filings",
               "append_prediction_tool_trace"):
        monkeypatch.setattr(news_mod, fn, _noop)

    state = pa.build_initial_state(
        "pred_inc", "admin", "",
        {"market": "cn_a", "symbol": "000001.SZ", "horizon": 7,
         "dimensions": ["macro_policy", "negative_event"]},
        status="news_searching",
    )
    asyncio.run(news_mod.NewsSearchNode().execute(state))

    # 每维各写一次，且**在 gather 返回之前**就写了（= 跑完一个落一个）
    assert appended == [("macro_policy", "search"), ("negative_event", "data")], (
        f"维度未按 kind 增量落库: {appended}"
    )
    assert finished_before_gather_returns == ["macro_policy", "negative_event"]
