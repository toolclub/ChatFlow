"""stock_kline — 个股 K 线 + MA 均线"""
GUIDANCE = (
    "查一只股票最近 N 个交易日的 K 线（OHLCV）+ 均线（MA5/MA10/MA20），趋势判断用。"
    "用户问「XX 这周走势」「XX 突破均线没」「最近多少天怎么样」时召唤。"
    "返回 dates / values / ma5 / ma10 / ma20 数组（数据是近期收盘价），让模型自己判断趋势。"
    "如只要当前价格用 stock_quote。"
)
ERROR_HINT = "如果 pending=true，说明缓存正在补；请告知用户「数据正在加载，稍后再问」。"
TAGS = ["finance", "kline"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger("tools.finance.stock_kline")


@tool
async def stock_kline(symbol: str, days: int = 60) -> str:
    """
    查询个股 K 线 + 均线数据（最近 N 个交易日）。

    Args:
        symbol: 股票代码。A 股 6 位（000001）或带后缀（000001.SZ）；美股加 ".US"。
        days:   返回多少日的数据，默认 60，最多 240。

    Returns:
        JSON 字符串：{
          "symbol","dates":[...],"values":[[open,close,low,high],...],
          "ma5":[...],"ma10":[...],"ma20":[...],"pending": bool
        }
    """
    try:
        from quant.providers.symbols import to_internal
        from quant.service import get_service

        sym = to_internal(symbol) if not symbol.endswith(".US") else symbol
        n = max(5, min(int(days), 240))
        service = get_service()
        data = await service.get_stock_chart_data(sym, days=n)
    except Exception as exc:
        logger.warning("stock_kline 调用失败 symbol=%s err=%s", symbol, exc)
        return json.dumps({"error": str(exc), "symbol": symbol}, ensure_ascii=False)

    return json.dumps(data, ensure_ascii=False)
