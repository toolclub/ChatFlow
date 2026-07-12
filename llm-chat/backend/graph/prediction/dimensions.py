"""预测维度注册表 —— 26 维的单一真相源（设计文档 §2.1/§2.2）。

v1 把 12 维搜索维度硬编码在 config.NEWS_DIMENSIONS + DIMENSION_LABELS 里，且无任何校验
（传垃圾 key 会照跑 Tavily 烧钱）。v2 统一收进本注册表：

  - kind="search" → 走 Tavily 资讯搜索（news_search.py 的 _run_dimension）
  - kind="data"   → 走结构化取数（fetcher 指向 quant.data.* 的函数名，不消耗 Tavily 预算）

`desc` 会**直接显示在前端的维度多选框上**，所以写人话、写清楚这一维到底影响什么，
不要写实现细节。

维度级降级（设计文档 §10-#4）：某维数据源挂掉（akshare 429 / SEC 403 / fetcher 未实现）
只把该维标 failed，其余维度继续跑；只有行情 preflight 失败才整单 failed。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 市场标识（与 domain.Market 同源，此处独立声明避免循环导入）
MarketKey = Literal["cn_a", "us_stock"]
DimensionKind = Literal["search", "data"]

_ALL_MARKETS: tuple[str, ...] = ("cn_a", "us_stock")


@dataclass(frozen=True)
class Dimension:
    """单个预测维度的元数据。frozen=True：注册表是常量，禁止运行期篡改。"""

    key: str
    label: str                       # 中文名（前端多选框标题）
    desc: str                        # 一句话作用，直接显示给用户（设计文档 §2.1）
    kind: DimensionKind              # "search"=Tavily 搜索型；"data"=结构化取数型
    markets: tuple[str, ...]         # 该维适用的市场
    fetcher: str | None = None       # kind="data" 时的取数函数名（也是前端展示的"工具名"）
    default_on: bool = True          # 是否默认勾选

    @property
    def name(self) -> str:
        """`label` 的别名 —— 兼容按 `name` 取中文名的调用方。"""
        return self.label

    def supports(self, market: str) -> bool:
        return market in self.markets


# ─────────────────────────────────────────────────────────────────────────────
# A. 数据型维度（结构化取数，不消耗 Tavily 预算）—— 默认全开
#    口径缺口是已知且用户已接受的（设计文档 §10-#5/#6）：
#    大股东减持无结构化源、A 股无官方处罚库 → 用「公告类型 + 标题 + LLM」补齐。
#    前端必须在数据卡片上标注来源与置信度，不假装精确。
# ─────────────────────────────────────────────────────────────────────────────
_DATA_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="announcements_full",
        label="全量历史公告",
        desc="拉取该股上市以来的全部公告标题、类型与日期，作为下面三个风险维度的共同数据底座；"
             "单独勾选时只做时间线展示",
        kind="data",
        markets=_ALL_MARKETS,
        fetcher="fetch_stock_notices",
        default_on=True,
    ),
    Dimension(
        key="negative_event",
        label="负面事件 / 爆雷",
        desc="扫描公告中的违法违规、监管问询、退市风险警示、股份冻结、业绩首亏/续亏等事件，"
             "按严重度打分并扩大预测波动率",
        kind="data",
        markets=_ALL_MARKETS,
        fetcher="fetch_negative_events",
        default_on=True,
    ),
    Dimension(
        key="private_placement",
        label="定增 / 增发",
        desc="识别定向增发、公开增发与配股，按摊薄比例和发行折价折算利空强度"
             "（增发摊薄每股权益，通常压制短期股价）",
        kind="data",
        markets=_ALL_MARKETS,
        fetcher="fetch_placements",
        default_on=True,
    ),
    Dimension(
        key="stake_reduction",
        label="股东减持",
        desc="识别董监高及大股东减持（含预披露/实施/完成），按减持比例与时间衰减折算抛压强度",
        kind="data",
        markets=_ALL_MARKETS,
        fetcher="fetch_reductions",
        default_on=True,
    ),
    Dimension(
        key="sec_filings",
        label="SEC 备案事件（美股）",
        desc="读取美股全部 SEC 备案：8-K Item 编号（4.02 财报不可靠为强负面）、XBRL 商誉减值数值、"
             "S-3/424B 增发、Form 4/144 内部人减持",
        kind="data",
        markets=("us_stock",),
        fetcher="fetch_sec_events",
        default_on=True,
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# B. v1 已跑通的 12 维 Tavily 搜索型 —— 默认全开，key 保持不变
#    （老数据、老测试仍按这些 key 断言，改 key = 破坏 v1 快照）
# ─────────────────────────────────────────────────────────────────────────────
_SEARCH_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="geopolitics",
        label="地缘政治/战争冲突",
        desc="战争、制裁、关税、供应链断供等地缘事件对该股的冲击",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="tech_breakthrough",
        label="科技突破/行业技术",
        desc="行业技术拐点（新工艺/新产品/专利）带来的估值重估",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="extreme_weather",
        label="极端天气/自然灾害",
        desc="台风、地震、限电等对产能与物流的短期扰动",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="earnings_guidance",
        label="财报/业绩指引",
        desc="最近财报与管理层指引是否超预期",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="regulatory_events",
        label="突发事件（监管/诉讼/高管）",
        desc="监管处罚、诉讼、高管离职等突发利空",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="macro_policy",
        label="宏观政策（利率/汇率/财政）",
        desc="降息降准、汇率、财政刺激对整体估值中枢的影响",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="supply_chain",
        label="供应链/大宗商品",
        desc="上游原材料涨跌与供应紧张度",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="market_sentiment",
        label="市场情绪（社交/股吧）",
        desc="散户与社媒热度，用于捕捉短期动量",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="peer_competitors",
        label="关联公司/竞品联动",
        desc="同业龙头或竞品的涨跌传导",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="capital_flow",
        label="资金面（北向/主力/融资）",
        desc="主力资金、北向资金、融资余额的流入流出",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="analyst_rating",
        label="分析师评级/目标价",
        desc="券商评级上下调与目标价变动",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
    Dimension(
        key="tail_risk",
        label="黑天鹅/尾部风险",
        desc="退市、财务造假、被制裁等极端风险",
        kind="search", markets=_ALL_MARKETS, default_on=True,
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# C. v1 列了但没实现的 9 维 —— v2 接入，默认关
#    lockup_buyback 按设计文档 §2.2-C 升级为数据型；其 fetcher 若尚未实现，
#    按维度级降级处理（该维标 failed，不影响其余维度）。
# ─────────────────────────────────────────────────────────────────────────────
_ADVANCED_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="lockup_buyback",
        label="限售解禁/回购",
        desc="未来 3 个月的解禁规模（抛压）与回购计划（托底）",
        kind="data",
        markets=("cn_a",),
        fetcher="fetch_lockup_buyback",
        default_on=False,
    ),
    Dimension(
        key="dividend_split",
        label="分红/送转/除权除息",
        desc="除权除息日的价格跳空与填权/贴权预期",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="options_volatility",
        label="期权/隐含波动率",
        desc="期权到期日与隐含波动率是否异常抬升",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="supply_chain_chain",
        label="产业链上下游传导",
        desc="上下游景气度沿产业链的传导",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="sector_leader",
        label="板块龙头异动",
        desc="所属板块龙头的异动是否会外溢到该股",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="index_rebalance",
        label="指数调整/纳入剔除",
        desc="纳入或剔除 MSCI、沪深 300 等指数带来的被动资金",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="shareholder_conc",
        label="股东户数/筹码集中度",
        desc="股东户数变化反映的筹码集中或分散",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="seasonality",
        label="季节性/日历效应",
        desc="财报季、春节效应、月末效应等日历规律",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
    Dimension(
        key="policy_calendar",
        label="政策事件日历",
        desc="未来 7 天内的政策、会议与数据发布日程",
        kind="search", markets=_ALL_MARKETS, default_on=False,
    ),
)


# ── 注册表（有序：数据型在前，前端按此顺序渲染分组）──────────────────────────────
DIMENSION_REGISTRY: dict[str, Dimension] = {
    d.key: d for d in (*_DATA_DIMENSIONS, *_SEARCH_DIMENSIONS, *_ADVANCED_DIMENSIONS)
}

# 前端分组（组名 → key 列表），供 GET /api/prediction/dimensions 直接下发
DIMENSION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("公司公告与事件", tuple(d.key for d in _DATA_DIMENSIONS)),
    ("资讯与市场", tuple(d.key for d in _SEARCH_DIMENSIONS)),
    ("进阶维度", tuple(d.key for d in _ADVANCED_DIMENSIONS)),
)


# ── 查询辅助 ──────────────────────────────────────────────────────────────────

def get_dimension(key: str) -> Dimension | None:
    """按 key 取维度定义；未注册返回 None（调用方决定是降级还是报错）。"""
    return DIMENSION_REGISTRY.get(key)


def all_keys() -> list[str]:
    """全部 26 个维度 key（注册顺序）。"""
    return list(DIMENSION_REGISTRY)


def default_dimensions(market: str | None = None) -> list[str]:
    """默认勾选的维度（default_on=True），可按市场过滤。

    market=None → 不过滤（17 维：5 数据 + 12 搜索）
    market="cn_a" → 16 维（sec_filings 仅美股，被过滤掉）
    market="us_stock" → 17 维
    """
    return [
        d.key for d in DIMENSION_REGISTRY.values()
        if d.default_on and (market is None or d.supports(market))
    ]


def dimensions_for_market(market: str) -> list[str]:
    """该市场支持的全部维度 key（含默认关闭的）。"""
    return [d.key for d in DIMENSION_REGISTRY.values() if d.supports(market)]


def search_keys() -> list[str]:
    """全部搜索型维度 key。"""
    return [d.key for d in DIMENSION_REGISTRY.values() if d.kind == "search"]


def data_keys() -> list[str]:
    """全部数据型维度 key。"""
    return [d.key for d in DIMENSION_REGISTRY.values() if d.kind == "data"]


def split_by_kind(keys: list[str]) -> tuple[list[str], list[str]]:
    """把一组 key 拆成 (搜索型, 数据型)，未注册的 key 直接丢弃。

    news_search 的分流入口：搜索型走 Tavily，数据型走 fetcher（不占 Tavily 预算）。
    """
    search: list[str] = []
    data: list[str] = []
    for k in keys:
        dim = DIMENSION_REGISTRY.get(k)
        if dim is None:
            continue
        (search if dim.kind == "search" else data).append(k)
    return search, data


def label_of(key: str) -> str:
    """中文名；未注册则回退 key 本身（保持 v1 DIMENSION_LABELS.get(k, k) 的语义）。"""
    dim = DIMENSION_REGISTRY.get(key)
    return dim.label if dim else key


def validate_dimensions(keys: list[str], market: str | None = None) -> tuple[list[str], list[str]]:
    """校验一组 key，返回 (合法的, 非法的)。

    非法 = 未注册 或 不支持该市场（如 A 股勾了 sec_filings）。
    去重且保持传入顺序。
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        dim = DIMENSION_REGISTRY.get(k)
        if dim is None or (market is not None and not dim.supports(market)):
            invalid.append(k)
        else:
            valid.append(k)
    return valid, invalid


def to_payload(market: str | None = None) -> list[dict]:
    """序列化给前端（GET /api/prediction/dimensions）。"""
    return [
        {
            "key": d.key,
            "label": d.label,
            "desc": d.desc,
            "kind": d.kind,
            "markets": list(d.markets),
            "fetcher": d.fetcher,
            "default_on": d.default_on,
        }
        for d in DIMENSION_REGISTRY.values()
        if market is None or d.supports(market)
    ]
