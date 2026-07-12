"""资讯搜索 helper（graph/prediction/news_search.py）—— 单元测试

验证 spec §news_search 行为：
  T-PRED-NEWS-01: _normalize_result 保留完整 raw_content（不 4500 切片）
  T-PRED-NEWS-02: tavily_search 在 ConnectError 下重试 TAVILY_RETRY_TIMES 次
  T-PRED-NEWS-03: generate_queries 解析 LLM astream 的 JSON 数组（1-2 query）
  T-PRED-NEWS-04: score_sentiment 解析 LLM astream 的 {sentiment,reason}
  T-PRED-NEWS-05: search_all_dimensions 12 维全量编排 + on_dimension 回调 + 均值钳制
  T-PRED-NEWS-06: per-prediction 预算超限返回降级 note（不调 httpx）

本轮「卡住感 / 慢」修复新增（12 维串行 6~16 分钟 → 并发 1~3 分钟）：
  T-PRED-NEWS-07: 12 维并发跑（Semaphore）—— 计数器测同时在飞的数量，断言并发度 > 1
  T-PRED-NEWS-08: 并发度受 concurrency 参数约束（concurrency=1 → 退化为串行，证明信号量是真的）
  T-PRED-NEWS-09: ★ 每维**开跑前**就发 PREDICTION_PROGRESS（第一条进度早于第一个 Tavily 调用）
  T-PRED-NEWS-10: 单维失败 → digest 标 failed，且**不被当成中性 0 分**拉低情绪均值
  T-PRED-NEWS-11: 全维失败 → 抛 NewsSearchUnavailable（不拿 12 个空维度继续跑预测）
  T-PRED-NEWS-12: 并发下 digest 顺序仍严格等于 dims 入参顺序（gather 保序）

设计原则（对齐 test_quant_analyze.py / test_quant_screen.py 隔离）：
  - 无 DB/Redis/外部网络：httpx.AsyncClient、_incr_prediction_count、
    get_chat_llm 全部 monkeypatch；asyncio.sleep 被替换为 no-op。
  - news_search 模块 import chain 不依赖 statsmodels/arch（实测 IMPORT OK），
    故无需 sys.modules 占位。
  - LLM 走 astream（铁律 #5）；fake LLM 按 messages[0] system prompt 分支。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
import pytest

import graph.prediction.news_search as ns
from graph.prediction.config import NEWS_DIMENSIONS, PREDICTION_WEBSEARCH_BUDGET


# ════════════════════════════════════════════════════════════════════════════
# Fake LLM（astream）：按 messages[0] system prompt 分支输出不同 JSON
# ════════════════════════════════════════════════════════════════════════════

_THINK_PREFIX = ns._THINK_PREFIX


class _FakeLLM:
    """模拟 LLMClient.astream。

    按 system prompt 关键词决定吐 query-gen JSON 数组 还是 sentiment JSON。
    支持可选 reasoning 前缀（验证 thinking token 剥离不污染 content 解析）。
    """

    def __init__(
        self,
        queries_json: str = '["q1","q2"]',
        sentiment_json: str = '{"sentiment":0.6,"reason":"看多"}',
        reasoning: str = "",
    ):
        self._queries = queries_json
        self._sentiment = sentiment_json
        self._reasoning = reasoning

    async def astream(self, messages, *args, **kwargs) -> AsyncIterator[str]:
        sys_content = ""
        if messages and isinstance(messages[0], dict):
            sys_content = messages[0].get("content", "")
        # query 生成器 vs 情绪分析器
        payload = self._sentiment if "情绪" in sys_content else self._queries
        # 先吐 thinking 段（若提供），每个 token 带 _THINK_PREFIX
        if self._reasoning:
            for ch in self._reasoning:
                yield _THINK_PREFIX + ch
        for ch in payload:
            yield ch


# ════════════════════════════════════════════════════════════════════════════
# Fake httpx.AsyncClient：post 行为可配置（ok payload / ConnectError）
# ════════════════════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """替换 news_search.httpx.AsyncClient。post() 行为由类属性控制。"""

    # 类级可配置状态（每测试重置）
    post_behavior: str = "ok"          # "ok" | "connect_error"
    post_calls: int = 0
    captured_json: list[dict] = []     # 记录每次 post 的 JSON payload（断言 days 等字段）
    payload: dict = {
        "answer": "综合摘要",
        "results": [
            {"title": "T1", "url": "http://u1", "content": "摘要1", "raw_content": "R" * 20000},
        ],
    }

    @classmethod
    def reset(cls, behavior: str = "ok", payload: dict | None = None):
        cls.post_behavior = behavior
        cls.post_calls = 0
        cls.captured_json = []
        if payload is not None:
            cls.payload = payload

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).post_calls += 1
        type(self).captured_json.append(json)
        if type(self).post_behavior == "connect_error":
            raise httpx.ConnectError("simulated connection refused")
        return _FakeResponse(type(self).payload)


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """避免 tavily_search 重试时的指数退避 sleep 真实阻塞。"""
    async def _nosleep(_t):
        return None
    monkeypatch.setattr(ns.asyncio, "sleep", _nosleep)


@pytest.fixture(autouse=True)
def _stub_httpx(monkeypatch):
    """把 news_search.httpx.AsyncClient 换成 _FakeAsyncClient。"""
    _FakeAsyncClient.reset()
    monkeypatch.setattr(ns.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.fixture(autouse=True)
def _stub_budget_ok(monkeypatch):
    """默认让 per-prediction 预算检查通过（计数=1，远低于上限）。"""
    async def _ok(_prediction_id):
        return 1
    monkeypatch.setattr(ns, "_incr_prediction_count", _ok)


@pytest.fixture(autouse=True)
def _stub_llm_factory(monkeypatch):
    """默认让 get_chat_llm 返回 _FakeLLM；可被单个测试覆盖工厂逻辑。"""
    def _factory(*_a, **_kw):
        return _FakeLLM()
    monkeypatch.setattr(ns, "get_chat_llm", _factory)


@pytest.fixture(autouse=True)
def _enable_tavily_key(monkeypatch):
    """让 TAVILY_API_KEY 视为已配置，避免 search_all_dimensions 短路。"""
    monkeypatch.setattr(ns, "TAVILY_API_KEY", "fake-key-for-test")


# ════════════════════════════════════════════════════════════════════════════
# 测试用例
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_normalize_keeps_full_raw_content():
    """T-PRED-NEWS-01：_normalize_result 保留完整 raw_content，不切 4500。"""
    big = "X" * 20000
    r = ns._normalize_result({
        "title": "t",
        "url": "http://u",
        "content": "摘要",
        "raw_content": big,
    })
    assert r["full_content"] == big
    assert len(r["full_content"]) == 20000, "full_content 被截断，违背全量不截取"
    # 结构字段齐
    assert r["title"] == "t"
    assert r["url"] == "http://u"
    assert r["snippet"] == "摘要"
    # 无 chunks 字段（与 web_search.py 路径区分）
    assert "chunks" not in r


@pytest.mark.unit
def test_tavily_search_retries_on_connect_error(monkeypatch):
    """T-PRED-NEWS-02：httpx ConnectError 时重试 TAVILY_RETRY_TIMES 次后降级。"""
    from graph.prediction.config import TAVILY_RETRY_TIMES

    _FakeAsyncClient.reset(behavior="connect_error")

    async def _run():
        return await ns.tavily_search("some query", "pred_retry")

    res = asyncio.run(_run())

    # 重试次数 == TAVILY_RETRY_TIMES（每次 attempt 都发起一次 post）
    assert _FakeAsyncClient.post_calls == TAVILY_RETRY_TIMES, (
        f"重试次数不符: {_FakeAsyncClient.post_calls}/{TAVILY_RETRY_TIMES}"
    )
    # 最终降级
    assert res["results"] == []
    assert "连接失败" in (res.get("note") or "")


@pytest.mark.unit
def test_tavily_search_ok_keeps_full_content():
    """补充：正常返回时 full_content 保留完整 raw_content。"""
    _FakeAsyncClient.reset(
        behavior="ok",
        payload={
            "answer": "A",
            "results": [
                {"title": "t", "url": "u", "content": "s", "raw_content": "Y" * 20000},
            ],
        },
    )

    async def _run():
        return await ns.tavily_search("q", "pred_ok")

    res = asyncio.run(_run())
    assert res["answer"] == "A"
    assert len(res["results"]) == 1
    assert len(res["results"][0]["full_content"]) == 20000


@pytest.mark.unit
def test_generate_queries_parses_json_array(monkeypatch):
    """T-PRED-NEWS-03：generate_queries 解析 astream 的 JSON 数组（1-2 query）。"""
    captured_deltas: list[str] = []

    async def _on_delta(d: str):
        captured_deltas.append(d)

    async def _run():
        return await ns.generate_queries(
            _FakeLLM(queries_json='["A股 地缘政治", "geopolitics risk"]'),
            symbol="000001.SZ", market="cn_a", dimension="geopolitics",
            on_delta=_on_delta,
        )

    queries = asyncio.run(_run())
    assert 1 <= len(queries) <= 2
    assert "A股 地缘政治" in queries
    # on_delta 只转发 reasoning token；该 FakeLLM 无 reasoning → content 不转发（进 content_parts 解析为 queries）
    assert captured_deltas == []


@pytest.mark.unit
def test_generate_queries_thinking_prefix_stripped(monkeypatch):
    """补充：推理模型 thinking token 剥离前缀后转发，不污染 JSON 解析。"""
    reasoning = "正在思考..."
    content = '["q1"]'
    seen_thinking: list[str] = []

    async def _on_delta(d: str):
        seen_thinking.append(d)

    async def _run():
        return await ns.generate_queries(
            _FakeLLM(queries_json=content, reasoning=reasoning),
            symbol="AAPL", market="us_stock", dimension="macro_policy",
            on_delta=_on_delta,
        )

    queries = asyncio.run(_run())
    assert queries == ["q1"]
    # thinking 段经 on_delta 转发（剥离了 _THINK_PREFIX）
    assert "".join(seen_thinking) == reasoning


@pytest.mark.unit
def test_generate_queries_fallback_on_empty_parse(monkeypatch):
    """补充：LLM 输出无法解析为 JSON 时兜底拼一条 query，不阻塞。"""
    async def _run():
        return await ns.generate_queries(
            _FakeLLM(queries_json='not a json at all'),
            symbol="000001.SZ", market="cn_a", dimension="earnings_guidance",
        )

    queries = asyncio.run(_run())
    assert len(queries) == 1
    assert "000001.SZ" in queries[0]


@pytest.mark.unit
def test_score_sentiment_parses_json(monkeypatch):
    """T-PRED-NEWS-04：score_sentiment 解析 astream 的 {sentiment,reason}。"""
    dim_result = {
        "dimension": "macro_policy",
        "answer": "降息预期升温",
        "results": [
            {"title": "t", "full_content": "c" * 500},
        ],
    }

    async def _run():
        return await ns.score_sentiment(
            _FakeLLM(sentiment_json='{"sentiment":0.6,"reason":"看多"}'),
            dimension_result=dim_result,
        )

    sentiment, reason = asyncio.run(_run())
    assert sentiment == pytest.approx(0.6)
    assert reason == "看多"


@pytest.mark.unit
def test_score_sentiment_clamps_to_range():
    """补充：sentiment 钳制到 [-1,1]，解析失败兜底 0.0。"""
    dim_result = {"dimension": "x", "answer": "", "results": []}

    async def _run_high():
        return await ns.score_sentiment(
            _FakeLLM(sentiment_json='{"sentiment":5.0,"reason":"爆多"}'), dim_result,
        )

    s, _ = asyncio.run(_run_high())
    assert s == 1.0  # 钳制

    async def _run_garbage():
        return await ns.score_sentiment(
            _FakeLLM(sentiment_json='我无法判断'), dim_result,
        )

    s0, r0 = asyncio.run(_run_garbage())
    assert s0 == 0.0
    assert r0 == ""


@pytest.mark.unit
def test_search_all_dimensions_orchestration(monkeypatch):
    """T-PRED-NEWS-05：12 维全量编排 + on_dimension 回调 + 均值钳制。"""
    tavily_calls: list[str] = []
    dim_callbacks: list[dict] = []

    async def _fake_tavily(query, prediction_id, max_results=10):
        tavily_calls.append(query)
        return {
            "answer": "综合答案",
            "results": [
                {"title": "t", "url": "u", "snippet": "s", "full_content": "fc" * 100},
            ],
        }

    monkeypatch.setattr(ns, "tavily_search", _fake_tavily)

    # get_chat_llm 已被 autouse fixture 桩成 _FakeLLM()，每维输出固定 query/sentiment
    async def _on_dim(d: dict):
        dim_callbacks.append(d)

    async def _run():
        return await ns.search_all_dimensions(
            symbol="000001.SZ", market="cn_a", prediction_id="pred_orch",
            on_dimension=_on_dim,
        )

    digest, news_sentiment = asyncio.run(_run())

    # 维度数 == NEWS_DIMENSIONS（默认 12 维）
    assert len(digest) == len(NEWS_DIMENSIONS)
    # on_dimension 每维触发一次
    assert len(dim_callbacks) == len(NEWS_DIMENSIONS)
    # 每条 digest 含必需字段
    for item in digest:
        assert {"dimension", "query", "answer", "results", "sentiment", "sentiment_reason"} <= set(item)
        assert -1.0 <= item["sentiment"] <= 1.0
    # 均值聚合在 [-1,1]
    assert -1.0 <= news_sentiment <= 1.0
    # tavily 至少被调用过
    assert tavily_calls


@pytest.mark.unit
def test_search_all_dimensions_custom_dims(monkeypatch):
    """补充：传入 dimensions 子集，digest 长度跟随。"""
    async def _fake_tavily(query, prediction_id, max_results=10):
        return {"answer": "a", "results": []}

    monkeypatch.setattr(ns, "tavily_search", _fake_tavily)

    async def _run():
        return await ns.search_all_dimensions(
            symbol="AAPL", market="us_stock", prediction_id="pred_sub",
            dimensions=["macro_policy", "earnings_guidance"],
        )

    digest, news_sentiment = asyncio.run(_run())
    assert len(digest) == 2
    assert [d["dimension"] for d in digest] == ["macro_policy", "earnings_guidance"]


@pytest.mark.unit
def test_budget_exhausted_returns_degraded_note(monkeypatch):
    """T-PRED-NEWS-06：计数 > PREDICTION_WEBSEARCH_BUDGET → 降级 note，不调 httpx。"""
    over = PREDICTION_WEBSEARCH_BUDGET + 1

    async def _over_budget(_prediction_id):
        return over

    monkeypatch.setattr(ns, "_incr_prediction_count", _over_budget)
    _FakeAsyncClient.reset(behavior="ok")  # 即使配置成 ok 也不应被调用

    async def _run():
        return await ns.tavily_search("q", "pred_budget")

    res = asyncio.run(_run())

    assert res["results"] == []
    assert "预算耗尽" in (res.get("note") or "")
    # 预算超限应在 httpx 之前短路
    assert _FakeAsyncClient.post_calls == 0, (
        f"预算超限不应调用 httpx，实际 post 调用 {_FakeAsyncClient.post_calls} 次"
    )


# ════════════════════════════════════════════════════════════════════════════
# 并发 + 进度事件（本轮「卡住感 / 慢」修复）
# ════════════════════════════════════════════════════════════════════════════
#
# 注意：autouse 的 _no_sleep fixture 把 asyncio.sleep 换成了 no-op（它 patch 的是真正的
# asyncio 模块），所以下面**不能**用 asyncio.sleep 来制造 await 悬挂点 —— 那不会让出事件
# 循环，并发度会被误测成 1。用一个裸 yield 的 awaitable 来强制交出控制权。


class _YieldOnce:
    """裸 await 点：把控制权交回事件循环一次（不依赖 asyncio.sleep）。"""

    def __await__(self):
        yield


async def _yield_control(times: int = 3) -> None:
    for _ in range(times):
        await _YieldOnce()


class _Probe:
    """记录事件时间线 + Tavily 在飞数量（用于测并发度与"进度先于搜索"）。"""

    def __init__(self) -> None:
        self.timeline: list[tuple[str, str]] = []   # (kind, detail)
        self.in_flight = 0
        self.max_in_flight = 0
        self.progress: list[dict] = []
        self.dimensions: list[dict] = []


def _install_fakes(monkeypatch, probe: _Probe, *, failing_dims: set[str] | None = None,
                   all_fail: bool = False):
    """把 generate_queries / tavily_search / score_sentiment 换成可控替身。

    query 里带上 dimension key，好让 tavily 替身按维度决定成功/失败。
    """
    failing = failing_dims or set()

    async def _fake_generate_queries(llm, symbol, market, dimension, on_delta=None, diag=None):
        return [f"q::{dimension}"]

    async def _fake_tavily(query, prediction_id, max_results=10):
        dim = query.split("::", 1)[-1]
        probe.timeline.append(("tavily", dim))
        probe.in_flight += 1
        probe.max_in_flight = max(probe.max_in_flight, probe.in_flight)
        try:
            await _yield_control(3)     # 模拟 HTTP 往返，强制让出事件循环
            if all_fail or dim in failing:
                # tavily_search 的降级形态：results 为空 + note 说明原因（不抛）
                return {"answer": "", "results": [], "note": "连接失败"}
            return {
                "answer": f"{dim} 综合答案",
                "results": [{"title": "t", "url": "u", "snippet": "s", "full_content": "fc"}],
            }
        finally:
            probe.in_flight -= 1

    async def _fake_score(llm, dimension_result, on_delta=None, diag=None):
        return 0.5, "看多"

    monkeypatch.setattr(ns, "generate_queries", _fake_generate_queries)
    monkeypatch.setattr(ns, "tavily_search", _fake_tavily)
    monkeypatch.setattr(ns, "score_sentiment", _fake_score)


async def _on_progress_factory(probe: _Probe):
    async def _on_progress(payload: dict) -> None:
        probe.timeline.append(("progress", payload.get("detail", "")))
        probe.progress.append(payload)
    return _on_progress


@pytest.mark.unit
def test_dimensions_run_concurrently(monkeypatch):
    """T-PRED-NEWS-07：12 维并发跑 —— 同时在飞的 Tavily 调用数 > 1。

    修复前 12 维严格串行（每维 1 LLM + 2 Tavily + 1 LLM），实测 6~16 分钟。
    """
    probe = _Probe()
    _install_fakes(monkeypatch, probe)

    async def _run():
        on_progress = await _on_progress_factory(probe)
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_conc",
            on_progress=on_progress, concurrency=4,
        )

    digest, sentiment = asyncio.run(_run())

    assert len(digest) == len(NEWS_DIMENSIONS)
    assert probe.max_in_flight > 1, (
        f"12 维仍是串行执行（最大并发 {probe.max_in_flight}）—— 用户还要等 6~16 分钟"
    )
    assert probe.max_in_flight <= 4, (
        f"并发度超出 Semaphore 上限（{probe.max_in_flight} > 4）—— 会打爆 Tavily/LLM 速率限制"
    )


@pytest.mark.unit
def test_concurrency_one_degrades_to_serial(monkeypatch):
    """T-PRED-NEWS-08：concurrency=1 → 最大并发恰为 1（证明信号量真的在限流，
    而不是上一条用例碰巧并发）。"""
    probe = _Probe()
    _install_fakes(monkeypatch, probe)

    async def _run():
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_serial",
            dimensions=["macro_policy", "geopolitics", "earnings_guidance"],
            concurrency=1,
        )

    digest, _ = asyncio.run(_run())
    assert len(digest) == 3
    assert probe.max_in_flight == 1, f"concurrency=1 应严格串行，实测 {probe.max_in_flight}"


@pytest.mark.unit
def test_progress_emitted_before_first_tavily_call(monkeypatch):
    """★ T-PRED-NEWS-09：每维**开跑前**就发进度（不是搜完才发）。

    第一条进度事件必须早于第一个 Tavily 调用 —— 这正是把 198 秒的 SSE 空洞
    填成「正在搜索：地缘政治 (3/12)」的关键。
    """
    probe = _Probe()
    _install_fakes(monkeypatch, probe)

    async def _run():
        on_progress = await _on_progress_factory(probe)
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_prog",
            on_progress=on_progress, concurrency=3,
        )

    digest, _ = asyncio.run(_run())
    total = len(NEWS_DIMENSIONS)

    kinds = [k for k, _ in probe.timeline]
    assert "progress" in kinds and "tavily" in kinds
    assert kinds.index("progress") < kinds.index("tavily"), (
        "第一条进度事件晚于第一个 Tavily 调用 —— SSE 上仍会出现长时间零字节空洞"
    )
    # 第一条进度就是"正在搜索：X"（开跑前那条）
    assert probe.timeline[0][0] == "progress"
    assert "正在搜索" in probe.timeline[0][1]

    # 每维至少 2 条进度（开跑前 + 完成后）
    assert len(probe.progress) >= total, (
        f"进度事件数 {len(probe.progress)} < 维度数 {total} —— 有维度没在开跑前上报"
    )
    assert len(probe.progress) == 2 * total

    # 契约 F 的 payload 结构
    for p in probe.progress:
        assert p["phase"] == "news_searching"
        assert p["total"] == total
        assert 0 <= p["index"] <= total
        assert p["detail"]

    # index 单调不减（并发下用的是"已完成维度数"的原子计数）
    idxs = [p["index"] for p in probe.progress]
    assert idxs == sorted(idxs)
    assert idxs[-1] == total, "收尾进度应为 total/total"


@pytest.mark.unit
def test_single_dimension_failure_is_flagged_not_neutral(monkeypatch):
    """★ T-PRED-NEWS-10：单维失败 → 标 failed，且**不被当成中性 0 分**混进情绪均值。

    修复前失败维度被默默当成 sentiment=0 喂进蒙特卡洛漂移修正 —— 那是把故障伪装成信号。
    """
    probe = _Probe()
    dims = ["macro_policy", "geopolitics", "earnings_guidance", "industry_chain"]
    _install_fakes(monkeypatch, probe, failing_dims={"geopolitics"})

    async def _run():
        on_progress = await _on_progress_factory(probe)
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_partial",
            dimensions=dims, on_progress=on_progress,
        )

    digest, news_sentiment = asyncio.run(_run())

    by_dim = {d["dimension"]: d for d in digest}
    bad = by_dim["geopolitics"]
    assert bad["failed"] is True, "失败维度必须标 failed"
    assert "连接失败" in bad["error"]
    # 原因要塞进 sentiment_reason：analyze_node 只把 dimension/sentiment/sentiment_reason
    # 三个字段喂给 LLM，它看不到 failed/error —— 不塞就等于告诉 LLM"这一维是中性"
    assert "搜索失败" in bad["sentiment_reason"]

    for ok_dim in ("macro_policy", "earnings_guidance", "industry_chain"):
        assert by_dim[ok_dim]["failed"] is False
        assert by_dim[ok_dim]["sentiment"] == pytest.approx(0.5)

    # ★ 均值只算 3 个成功维（全 0.5）→ 0.5。若失败维被当成中性 0 分，均值会被拉到 0.375
    assert news_sentiment == pytest.approx(0.5), (
        f"失败维度被当成中性 0 分拉低了情绪均值（得到 {news_sentiment}，应为 0.5）"
    )

    # 进度事件里失败维度有明确文案
    details = [p["detail"] for p in probe.progress]
    assert any("搜索失败" in d for d in details)


@pytest.mark.unit
def test_all_dimensions_failed_raises(monkeypatch):
    """★ T-PRED-NEWS-11：全维失败 → 抛 NewsSearchUnavailable。

    不能拿 12 个"中性 0 分"的空维度继续跑预测（那会让用户以为资讯已纳入，实际全是噪声）。
    节点据此置 state.error → 图短路 → 落 failed。
    """
    probe = _Probe()
    _install_fakes(monkeypatch, probe, all_fail=True)

    async def _run():
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_allfail",
            dimensions=["macro_policy", "geopolitics"],
        )

    with pytest.raises(ns.NewsSearchUnavailable) as ei:
        asyncio.run(_run())

    assert "资讯搜索全部失败" in ei.value.message
    assert len(ei.value.failures) == 2
    assert all(f["failed"] for f in ei.value.failures)


@pytest.mark.unit
def test_digest_order_matches_dims_under_concurrency(monkeypatch):
    """T-PRED-NEWS-12：并发（gather 保序）下 digest 顺序仍严格等于 dims 入参顺序。"""
    probe = _Probe()
    dims = ["industry_chain", "macro_policy", "geopolitics", "earnings_guidance"]
    _install_fakes(monkeypatch, probe)

    async def _run():
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_order",
            dimensions=dims, concurrency=4,
        )

    digest, _ = asyncio.run(_run())
    assert [d["dimension"] for d in digest] == dims, "并发后 digest 顺序被打乱"


@pytest.mark.unit
def test_missing_tavily_key_degrades_without_raise(monkeypatch):
    """TAVILY_API_KEY 未配置 → 全维标 failed 降级返回（不 raise）。

    配置缺失是确定性的部署问题而非瞬时故障：直接 raise 会让没配 Tavily 的部署
    整个预测功能不可用；降级后预测仍能跑技术面。
    """
    probe = _Probe()
    _install_fakes(monkeypatch, probe)
    monkeypatch.setattr(ns, "TAVILY_API_KEY", "")

    async def _run():
        on_progress = await _on_progress_factory(probe)
        return await ns.search_all_dimensions(
            symbol="MU", market="us_stock", prediction_id="pred_nokey",
            on_progress=on_progress,
        )

    digest, sentiment = asyncio.run(_run())

    assert len(digest) == len(NEWS_DIMENSIONS)
    assert all(d["failed"] for d in digest)
    assert all("TAVILY_API_KEY" in d["error"] for d in digest)
    assert sentiment == 0.0
    tavily_calls = [e for e in probe.timeline if e[0] == "tavily"]
    assert tavily_calls == [], "未配置 key 时不应发起任何 Tavily 请求"
    # 但必须把"为什么跳过"讲出来，而不是静默降级（铁律 #9）
    skips = [d for k, d in probe.timeline if k == "progress" and "TAVILY_API_KEY" in d]
    assert skips, "跳过资讯搜索时必须发进度事件说明原因"


@pytest.mark.unit
def test_build_progress_payload_shape():
    """契约 F：进度 payload 结构（phase / label / index / total / detail / dimension）。"""
    p = ns.build_progress("地缘政治", 3, 12, "正在搜索：地缘政治", "geopolitics")
    assert p == {
        "phase": "news_searching",
        "label": "地缘政治",
        "index": 3,
        "total": 12,
        "detail": "正在搜索：地缘政治",
        "dimension": "geopolitics",
    }
