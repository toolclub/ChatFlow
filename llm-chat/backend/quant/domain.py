"""量化模块领域模型 — Pydantic v2

只定义业务实体和参数结构，不依赖具体 SDK 或 DB。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ── 枚举 ──────────────────────────────────────────────────────────────────────

class ProviderCapability(str, Enum):
    """provider 能力枚举（决定 registry 把请求路由到哪个 provider）"""
    STOCK_LIST = "stock_list"            # 股票列表（symbol/name/industry）
    REALTIME_SNAPSHOT = "realtime_snapshot"  # 全市场快照（最新价/PE/PB/市值/成交额/涨跌幅等）
    DAILY_BARS = "daily_bars"            # 日线行情
    FUNDAMENTALS = "fundamentals"        # 财务指标（ROE/ROA/营收等）
    MONEY_FLOW = "money_flow"            # 资金流
    INDEX_WEIGHT = "index_weight"        # 指数成分股
    TRADING_CALENDAR = "trading_calendar"


class ProviderHealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


# ── 实体 ──────────────────────────────────────────────────────────────────────

class Stock(BaseModel):
    """统一股票标识（内部一律使用带后缀的格式：000001.SZ / 600000.SH）"""
    symbol: str
    name: str = ""
    market: str = "cn_a"
    industry: str = ""
    list_date: str = ""


class FactorScore(BaseModel):
    """单只股票的因子分和综合分"""
    symbol: str
    name: str = ""
    industry: str = ""
    technical: float = 0.0
    fundamental: float = 0.0
    liquidity: float = 0.0
    risk: float = 0.0
    total: float = 0.0
    rank: int = 0
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict, description="原始因子值（PE/PB/动量/波动率等）")


class ProviderHealth(BaseModel):
    """provider 健康检查结果"""
    status: ProviderHealthStatus
    message: str = ""


class ProviderInfo(BaseModel):
    """对外暴露的 provider 元信息（不含 token / 凭据）"""
    name: str
    enabled: bool
    priority: int
    capabilities: list[str]
    health: ProviderHealthStatus
    message: str = ""


class ProviderTrace(BaseModel):
    """一次 provider 调用记录（用于审计和前端展示）"""
    provider: str
    capability: str
    status: Literal["ok", "fallback", "error"]
    elapsed_ms: float = 0.0
    rows: int = 0
    error: str = ""


# ── 请求 / 响应 ────────────────────────────────────────────────────────────────

class ScreenCriteria(BaseModel):
    """选股请求参数"""
    market: Literal["cn_a", "us_stock"] = "cn_a"
    universe: Literal["all", "hs300", "zz500", "nasdaq", "sp500", "custom"] = "all"
    custom_symbols: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)

    # 硬过滤
    min_market_cap: float | None = Field(None, description="最低总市值（亿元）")
    min_avg_turnover: float | None = Field(None, description="近 20 日均成交额下限（亿元）")
    pe_range: tuple[float, float] | None = None
    pb_range: tuple[float, float] | None = None
    roe_min: float | None = None

    # 因子窗口
    momentum_window: int = 60
    volatility_window: int = 20

    # 风险过滤
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_new_stocks_days: int = 60

    # 输出
    top_n: int = 30
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "technical": 0.35,
            "fundamental": 0.35,
            "liquidity": 0.20,
            "risk": 0.10,
        },
        description="因子权重（须包含 technical/fundamental/liquidity/risk 四个 key）",
    )


class ScreenResult(BaseModel):
    """选股响应"""
    snapshot_id: str
    criteria: ScreenCriteria
    rows: list[FactorScore]
    provider_trace: list[ProviderTrace]
    weights: dict[str, float]
    universe_size: int
    as_of_date: str
    generated_at: float
    warnings: list[str] = Field(default_factory=list)
    analysis: str = ""
    risk_notes: list[str] = Field(default_factory=list)
