"""量化分析（thinking 透传）—— 单元测试

验证 spec §模型思考流程在 quant_analyze 节点上的合规性：
  1. emit_thinking 同时推送 reasoning + content 两个 phase
  2. stream_analyze 把 reasoning → "thinking" 事件、content → "delta" 事件
  3. done 事件携带 thinking_segments（与 messages.thinking_segments 同形）
  4. 空 rows 短路：直接 done，不调 LLM
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

import graph.quant_agent as quant_agent
from graph.nodes.base import BaseNode


# ── 通用 fake LLM：按预设序列产出 deltas ──────────────────────────────────────

class _FakeLLM:
    """模拟 LLMClient.astream：先吐 reasoning_content（带 \\x00THINK\\x00 前缀），
    再吐 content（最终 JSON 字符串）。"""

    def __init__(self, reasoning: str, content: str):
        self._reasoning = reasoning
        self._content = content

    async def astream(self, messages, temperature=0.3, timeout=120.0) -> AsyncIterator[str]:
        prefix = BaseNode._THINK_PREFIX
        # 推理段（chunk 拆细，模拟真实流式）
        for ch in self._reasoning:
            yield prefix + ch
        # 最终内容
        for ch in self._content:
            yield ch


@pytest.fixture
def _patch_llm(monkeypatch):
    """把 graph.nodes.quant.analyze_node.get_chat_llm 替换为 fake，
    避免单测真实调远程模型。"""
    from graph.nodes.quant import analyze_node as an_mod

    def _factory(reasoning: str, content: str):
        def _get(*_a, **_kw):
            return _FakeLLM(reasoning, content)
        monkeypatch.setattr(an_mod, "get_chat_llm", _get)

    return _factory


@pytest.fixture
def _patch_persist(monkeypatch):
    """拦截 update_quant_analysis，避免真实 DB 写入；同时记录调用。"""
    captured: dict = {}

    async def _fake_update(snapshot_id, analysis, risk_notes, thinking_segments=None):
        captured["snapshot_id"] = snapshot_id
        captured["analysis"] = analysis
        captured["risk_notes"] = list(risk_notes)
        captured["thinking_segments"] = list(thinking_segments or [])

    monkeypatch.setattr(quant_agent, "update_quant_analysis", _fake_update)
    return captured


# ── 测试用例 ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_stream_analyze_emits_thinking_and_delta(_patch_llm, _patch_persist):
    """T-QUANT-ANALYZE-01：
    LLM 推送 reasoning + content，stream_analyze 应分别 yield 'thinking' / 'delta' 事件。
    """
    reasoning_text = "推理"
    content_text = '{"analysis":"概况","risk_notes":["风险A"]}'
    _patch_llm(reasoning_text, content_text)

    rows = [{"symbol": "000001.SZ", "name": "示例", "total": 80.0, "reasons": ["A"]}]

    async def _run() -> list[dict]:
        out: list[dict] = []
        async for ev in quant_agent.stream_analyze(
            "snap_test", {"market": "cn_a"}, rows, top_n_for_llm=1,
        ):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    types = [e["event"] for e in events]

    # 至少有 thinking、delta、done
    assert "thinking" in types, f"缺 thinking 事件: {types}"
    assert "delta" in types, f"缺 delta 事件: {types}"
    assert types[-1] == "done", f"末尾不是 done: {types}"

    # thinking 累积出推理文本
    thinking_concat = "".join(e["text"] for e in events if e["event"] == "thinking")
    assert thinking_concat == reasoning_text

    # delta 累积出最终内容
    delta_concat = "".join(e["text"] for e in events if e["event"] == "delta")
    assert delta_concat == content_text

    # done 事件结构化字段齐全
    done = events[-1]
    assert done["analysis"] == "概况"
    assert done["risk_notes"] == ["风险A"]
    segs = done["thinking_segments"]
    phases = [s["phase"] for s in segs]
    assert "reasoning" in phases and "content" in phases
    for s in segs:
        assert s["node"] == "quant_analyze"
        assert s["step_index"] is None

    # 持久化拦截到正确数据
    assert _patch_persist["analysis"] == "概况"
    assert _patch_persist["risk_notes"] == ["风险A"]
    assert len(_patch_persist["thinking_segments"]) == 2


@pytest.mark.unit
def test_stream_analyze_empty_rows_short_circuits(monkeypatch, _patch_persist):
    """T-QUANT-ANALYZE-02：rows 为空时直接 done，不调 LLM。"""
    # 任何 LLM 调用都应该报错以验证短路
    from graph.nodes.quant import analyze_node as an_mod

    def _explode(*_a, **_kw):
        raise AssertionError("空 rows 不应该调用 LLM")
    monkeypatch.setattr(an_mod, "get_chat_llm", _explode)

    async def _run() -> list[dict]:
        out: list[dict] = []
        async for ev in quant_agent.stream_analyze("snap_empty", {}, []):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    assert len(events) == 1
    assert events[0]["event"] == "done"
    assert events[0]["analysis"] == "未筛选到任何符合条件的股票。"
    assert events[0]["risk_notes"] == []
    assert events[0]["thinking_segments"] == []
