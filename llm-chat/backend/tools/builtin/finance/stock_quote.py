"""stock_quote — 单只股票实时快照"""
GUIDANCE = (
    "查一只 A 股或美股的实时行情：最新价 / 涨跌幅 / 60 日涨幅 / 年初涨幅 / 成交额 / PE / PB / 市值 / 换手率。"
    "用户问「XX 现在多少钱」「XX 今天涨多少」「XX 最近表现」时召唤。"
    "数据来自磁盘缓存（数分钟级别新鲜度），如要 K 线趋势改用 stock_kline。"
)
ERROR_HINT = "若返回 not_found，先确认 symbol 拼写（A 股 6 位数字，美股 .US 后缀）。"
TAGS = ["finance", "quote"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger("tools.finance.stock_quote")


@tool
async def stock_quote(symbol: str) -> str:
    """
    查询单只股票实时行情快照。

    Args:
        symbol: 股票代码。A 股 6 位（000001）或带后缀（000001.SZ）；美股加 ".US" 后缀。

    Returns:
        JSON 字符串：{"symbol","name","price","change_pct","change_pct_60d",
                     "change_pct_ytd","amount","pe","pb","market_cap","turnover_rate",
                     "as_of_date"}；找不到时返回 {"error":"not_found"}。
    """
    try:
        from quant.providers.symbols import to_internal
        from quant.service import get_service

        sym = to_internal(symbol) if not symbol.endswith(".US") else symbol
        service = get_service()
        market = "us_stock" if sym.endswith(".US") else "cn_a"
        df = await service._adapter.spot(market=market, readonly=True)
        if df is None or df.empty:
            return json.dumps({"error": "snapshot_unavailable", "symbol": sym}, ensure_ascii=False)

        row = df[df["symbol"] == sym]
        if row.empty:
            return json.dumps({"error": "not_found", "symbol": sym}, ensure_ascii=False)

        r = row.iloc[0]

        def _v(k):
            v = r.get(k)
            try:
                return None if v is None or (hasattr(v, "isnan") and v.isnan()) else (
                    float(v) if isinstance(v, (int, float)) else str(v)
                )
            except Exception:
                return str(v) if v is not None else None

        out = {
            "symbol": sym,
            "name": _v("name"),
            "price": _v("price"),
            "change_pct": _v("change_pct"),
            "change_pct_60d": _v("change_pct_60d"),
            "change_pct_ytd": _v("change_pct_ytd"),
            "amount": _v("amount"),
            "pe": _v("pe"),
            "pb": _v("pb"),
            "market_cap": _v("market_cap"),
            "turnover_rate": _v("turnover_rate"),
            "as_of_date": _v("as_of_date"),
        }
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:
        logger.warning("stock_quote 调用失败 symbol=%s err=%s", symbol, exc)
        return json.dumps({"error": str(exc), "symbol": symbol}, ensure_ascii=False)
