"""stock_money_flow — 个股资金流"""
GUIDANCE = (
    "查一只 A 股最近 5 个交易日的主力 / 超大单 / 大单资金净流入数据。"
    "用户问「XX 主力在不在拉」「XX 资金面怎么样」「有没有北向 / 主力买入」时召唤。"
    "正向且持续主力净流入 + 涨幅 = 资金在抢筹；涨而流出 = 散户接盘，谨慎。"
)
ERROR_HINT = "akshare 资金流接口偶发返回空；可换问 stock_quote 看价格 + 成交量。"
TAGS = ["finance", "money_flow"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

from quant.data import money_flow as money_flow_mod

logger = logging.getLogger("tools.finance.stock_money_flow")


@tool
async def stock_money_flow(symbol: str) -> str:
    """
    查询个股资金流（主力 / 超大单 / 大单）—— 最近 5 个交易日 + 当日。

    Args:
        symbol: 股票代码，6 位（000001）或带后缀（000001.SZ）。

    Returns:
        JSON 字符串：{
          "today": {"date","main_net_inflow","main_net_inflow_pct",...},
          "history": [...]
        }
    """
    try:
        data = await money_flow_mod.fetch_money_flow(symbol)
    except Exception as exc:
        logger.warning("stock_money_flow 调用失败 symbol=%s err=%s", symbol, exc)
        return json.dumps({"error": f"获取失败：{exc}"}, ensure_ascii=False)

    return json.dumps({"symbol": symbol, **data}, ensure_ascii=False)
