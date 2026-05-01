"""MarketDataProvider Protocol — provider 抽象层

所有 provider 实现都遵循这个协议，service 层不直接 import 任何 SDK。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    Stock,
)


@runtime_checkable
class MarketDataProvider(Protocol):
    """市场数据 provider 抽象接口

    实现要求：
      - 同步 SDK 调用必须用 asyncio.to_thread 包装
      - 返回的 DataFrame 列名按本协议归一化（不要透出 SDK 原生中文列名）
      - 错误以 Python 异常抛出，由 registry 决定是否 fallback
    """

    name: str
    priority: int
    capabilities: set[ProviderCapability]

    async def health_check(self) -> ProviderHealth: ...

    async def list_stocks(self, market: str = "cn_a") -> list[Stock]: ...

    async def realtime_snapshot(self, market: str = "cn_a") -> pd.DataFrame:
        """全市场行情快照。

        归一化列：
          symbol, name, price, change_pct, change_pct_60d, change_pct_ytd,
          volume, amount, amplitude, turnover_rate, volume_ratio,
          pe, pb, market_cap (亿元), circ_market_cap (亿元),
          as_of_date (YYYY-MM-DD)
        """
        ...

    async def daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """日线 OHLCV。

        归一化列：
          symbol, date, open, high, low, close, volume, amount,
          amplitude, change_pct, turnover_rate
        """
        ...

    async def index_constituents(self, index_code: str) -> list[str]:
        """指数成分股（hs300 / zz500 / zz1000 或具体指数代码 000300 等）"""
        ...
