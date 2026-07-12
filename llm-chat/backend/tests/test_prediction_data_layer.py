"""数据层护栏（quant/data/announcement.py · sec_edgar.py）—— 纯 mock 单元测试。

★ 一行真网络都不打：sys.modules 注入假 akshare，_em_report / httpx 全部桩掉。
  真实取数已在容器里实测过（000001.SZ notices=1733 / 300750.SZ negative=10 /
  601288.SH 全 0 不崩），这里只做**回归护栏**，防止有人把 KeyError 兜底改回去。

  T-PRED-DATA-01  _ak_frame：KeyError → 空表（akshare「查到 0 条不返回空表」的老 bug）
  T-PRED-DATA-02  _ak_frame：KeyError 不重试（0 条不是故障，重试纯属浪费 3 秒）
  T-PRED-DATA-03  fetch_stock_notices：KeyError('代码') → [] 且不抛
  T-PRED-DATA-04  fetch_negative_events：公告 + 业绩预告双 KeyError → []（干净票 601288 场景）
  T-PRED-DATA-05  fetch_placements：stock_qbzf_em KeyError → []
  T-PRED-DATA-06  fetch_reductions：KeyError → []
  T-PRED-DATA-07  ★★ 反面：网络故障 → 抛 AnnouncementDataError，**绝不**降级成 []
                  （把「数据源挂了」说成「这只票很干净」是本系统最危险的谎）
  T-PRED-DATA-08  _ak_frame：akshare 缺函数（版本漂移）→ 抛错，不静默返回空表
  T-PRED-DATA-09  _ak_frame：返回 None → 空表
  T-PRED-DATA-10  classify_notice / title_hash / strip_highlight 契约

为什么必须有 T-PRED-DATA-07：KeyError→[] 和 网络错→[] 只差一个 except 分支，
但语义天差地别。前者「这只票没有减持公告」，后者「我们不知道有没有」。
一旦有人图省事把 except Exception 也 return []，UI 会对着 429 限流显示「零风险」。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pandas as pd
import pytest

import quant.data.announcement as A

pytestmark = pytest.mark.unit


# ── 假 akshare ────────────────────────────────────────────────────────────────

# 本模块真正会调到的 akshare 接口（假模块只认这些；其余名字 → AttributeError，
# 模拟「akshare 升级后接口改名」的真实故障）
_KNOWN_AK_API = {
    "stock_individual_notice_report",
    "stock_yjyg_em",
    "stock_qbzf_em",
    "stock_ggcg_em",
    "stock_news_em",
    "stock_research_report_em",
}


class _FakeAk:
    """假 akshare 模块：每个接口按 behaviors[func_name] 指定的行为响应。"""

    __version__ = "1.18.64-fake"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.behaviors: dict[str, object] = {}

    def _make(self, name: str):
        def _fn(*_a, **kw):
            self.calls.append((name, kw))
            b = self.behaviors.get(name, "empty")
            if isinstance(b, BaseException):
                raise b
            if callable(b):
                return b()
            if isinstance(b, pd.DataFrame):
                return b
            if b == "none":
                return None
            return pd.DataFrame()
        return _fn

    def __getattr__(self, name: str):
        if name in _KNOWN_AK_API:
            return self._make(name)
        raise AttributeError(name)      # 接口不存在（版本漂移）→ _ak_frame 必须显式报错

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


@pytest.fixture
def fake_ak(monkeypatch) -> _FakeAk:
    """注入假 akshare + 关掉退避 sleep + 清空全市场表缓存（否则用例间互相污染）。"""
    ak = _FakeAk()
    monkeypatch.setitem(sys.modules, "akshare", ak)
    monkeypatch.setattr(A, "AK_BACKOFF_BASE", 0.0)      # 重试退避 1s/2s → 0，别让单测跑 3 秒
    A._TABLE_CACHE.clear()                              # 全市场表 30min TTL 缓存
    yield ak
    A._TABLE_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_em_report(monkeypatch):
    """东财 JSON 接口（httpx）默认返回空 —— 本文件只测 akshare 那条路径。"""
    async def _empty(*_a, **_kw):
        return []
    monkeypatch.setattr(A, "_em_report", _empty)


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DATA-01 / 02 / 08 / 09：_ak_frame 护栏本身
# ══════════════════════════════════════════════════════════════════════════════

def test_ak_frame_keyerror_returns_empty_frame(fake_ak):
    """T-PRED-DATA-01：KeyError('代码') = 查到 0 条，不是故障 → 空表。"""
    fake_ak.behaviors["stock_individual_notice_report"] = KeyError("代码")

    df = asyncio.run(A._ak_frame("stock_individual_notice_report", symbol="601288"))

    assert isinstance(df, pd.DataFrame) and df.empty


def test_ak_frame_keyerror_does_not_retry(fake_ak):
    """T-PRED-DATA-02：0 条结果不该触发退避重试（重试 2 次 = 白等 3 秒还是 0 条）。"""
    fake_ak.behaviors["stock_qbzf_em"] = KeyError("减持")

    df = asyncio.run(A._ak_frame("stock_qbzf_em"))

    assert df.empty
    assert fake_ak.count("stock_qbzf_em") == 1, (
        f"KeyError 被当成故障重试了 {fake_ak.count('stock_qbzf_em')} 次"
    )


def test_ak_frame_missing_function_raises(fake_ak):
    """T-PRED-DATA-08：akshare 版本漂移导致接口消失 → 显式抛错，不静默返回空表。"""
    with pytest.raises(A.AnnouncementDataError):
        asyncio.run(A._ak_frame("stock_this_api_was_removed"))


def test_ak_frame_none_return_is_empty_frame(fake_ak):
    """T-PRED-DATA-09：接口返回 None → 按空表处理（下游 df.empty 不会 AttributeError）。"""
    fake_ak.behaviors["stock_news_em"] = "none"

    df = asyncio.run(A._ak_frame("stock_news_em", symbol="000001"))

    assert isinstance(df, pd.DataFrame) and df.empty


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DATA-03~06：四个 fetcher 的 KeyError 兜底（干净票不崩）
# ══════════════════════════════════════════════════════════════════════════════

def test_fetch_stock_notices_survives_keyerror(fake_ak):
    """T-PRED-DATA-03：0 条公告 → [] 且不抛（akshare 源码 stock_notice.py:118 未判空）。"""
    fake_ak.behaviors["stock_individual_notice_report"] = KeyError("代码")

    out = asyncio.run(A.fetch_stock_notices("601288.SH", market="cn_a", months=24))

    assert out == []


def test_fetch_negative_events_clean_stock_returns_empty(fake_ak):
    """T-PRED-DATA-04：公告 + 业绩预告双双 0 条 → []（601288 农业银行 negative=0 的真实场景）。"""
    fake_ak.behaviors["stock_individual_notice_report"] = KeyError("代码")
    fake_ak.behaviors["stock_yjyg_em"] = KeyError("股票代码")

    out = asyncio.run(A.fetch_negative_events("601288.SH", market="cn_a", months=24))

    assert out == [], "干净票必须返回空列表而不是崩掉整个维度"


def test_fetch_placements_survives_keyerror(fake_ak):
    """T-PRED-DATA-05：全市场增发表 0 条 → []。"""
    fake_ak.behaviors["stock_qbzf_em"] = KeyError("代码")
    fake_ak.behaviors["stock_individual_notice_report"] = KeyError("代码")

    out = asyncio.run(A.fetch_placements("601288.SH", market="cn_a", months=24))

    assert out == []


def test_fetch_reductions_survives_keyerror(fake_ak):
    """T-PRED-DATA-06：减持 —— 设计文档原写的 symbol='减持' 会 KeyError('减持')（已实测）。"""
    fake_ak.behaviors["stock_ggcg_em"] = KeyError("减持")
    fake_ak.behaviors["stock_individual_notice_report"] = KeyError("代码")

    out = asyncio.run(A.fetch_reductions("601288.SH", market="cn_a", months=24))

    assert out == []


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DATA-07：★ 反面 —— 数据源挂了绝不能伪装成「干净票」
# ══════════════════════════════════════════════════════════════════════════════

def test_network_failure_raises_not_empty(fake_ak):
    """T-PRED-DATA-07：429/超时 → 重试耗尽 → AnnouncementDataError。

    这是本文件最重要的一条：KeyError→[] 与 网络错→[] 只差一个 except 分支，
    语义却是「这只票没有减持」vs「我们不知道有没有」。降级成 [] 会让 UI 对着
    限流显示「零风险」—— 上游维度必须收到异常才能标 failed。
    """
    fake_ak.behaviors["stock_individual_notice_report"] = RuntimeError("HTTP 429 Too Many Requests")

    with pytest.raises(A.AnnouncementDataError) as ei:
        asyncio.run(A._ak_frame("stock_individual_notice_report", symbol="000001"))

    assert "429" in str(ei.value)
    # 初次 + AK_RETRY_TIMES 次重试
    assert fake_ak.count("stock_individual_notice_report") == A.AK_RETRY_TIMES + 1


def test_fetch_stock_notices_propagates_data_error(fake_ak):
    """数据源故障必须冒泡到 fetcher 之外（news_search._run_data_dimension 据此标 failed）。"""
    fake_ak.behaviors["stock_individual_notice_report"] = RuntimeError("connection reset")

    with pytest.raises(A.AnnouncementDataError):
        asyncio.run(A.fetch_stock_notices("000001.SZ", market="cn_a"))


def test_transient_failure_then_success_recovers(fake_ak):
    """一次抖动后重试成功 → 正常返回（重试不是摆设）。"""
    seq = [RuntimeError("timeout"), pd.DataFrame({"代码": ["000001"]})]

    def _flaky():
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    fake_ak.behaviors["stock_yjyg_em"] = _flaky

    df = asyncio.run(A._ak_frame("stock_yjyg_em", date="20260331"))

    assert not df.empty
    assert fake_ak.count("stock_yjyg_em") == 2


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DATA-10：规则分类工具函数契约（announcements.py 第 1 层依赖它）
# ══════════════════════════════════════════════════════════════════════════════

def test_classify_notice_marks_noise_and_hard_risk():
    """噪音（治理类）→ is_noise；退市风险 → 规则定级 + 不需要 LLM。"""
    noise = A.classify_notice("董事会决议公告", "第八届董事会第五次会议决议公告")
    assert noise["is_noise"] is True, "治理类公告必须在规则层丢弃（0 次 LLM）"

    risk = A.classify_notice("实施退市风险警示", "关于公司股票被实施退市风险警示的公告")
    assert risk["is_noise"] is not True
    assert risk["needs_llm"] is not True, "退市风险是结构化硬字段，不该再烧 LLM"
    assert risk["severity"] is not None and risk["severity"] < 0

    fuzzy = A.classify_notice("其它风险提示公告", "关于收到监管问询函的公告")
    assert fuzzy["needs_llm"] is True, "模糊标题应进候选池交给 LLM 精判"


def test_classify_notice_assigns_single_event_type():
    """★ 每条公告只贴**一个** event_type —— 三个风险维度的候选池据此天然互斥，不会重复判。"""
    for ann_type, title in [
        ("违法违规", "关于公司被立案调查的公告"),
        ("增发", "关于非公开发行股票预案的公告"),
        ("股东减持", "关于持股 5% 以上股东减持股份的公告"),
    ]:
        v = A.classify_notice(ann_type, title)
        assert isinstance(v.get("event_type"), str) and v["event_type"]


def test_title_hash_stable_and_strip_highlight():
    """title_hash 是缓存键（必须稳定）；strip_highlight 剥掉东财的高亮标签。"""
    clean = A.strip_highlight("关于<em>立案调查</em>的公告")
    assert "<em>" not in clean and "立案调查" in clean

    assert A.title_hash("同一个标题") == A.title_hash("同一个标题")
    assert A.title_hash("标题 A") != A.title_hash("标题 B")
