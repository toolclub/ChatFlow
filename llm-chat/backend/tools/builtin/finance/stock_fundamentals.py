"""stock_fundamentals — 个股财务详情"""
GUIDANCE = (
    "查一只 A 股的「估值 + 财务摘要」：PE / PB / 市值 + 最近 4 期营收/利润/ROE/毛利率。"
    "用户问「XX 基本面怎么样」「XX 业绩如何」「XX ROE / 利润趋势」时召唤。"
    "返回 4 个季度的财务数列，请基于趋势（同比 / 环比）判断成长性，不要凭记忆。"
)
ERROR_HINT = "akshare 财务摘要接口偶发返回空；如缺数据可补一句「数据暂未披露」让用户知情。"
TAGS = ["finance", "fundamentals"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

from quant.data import fundamentals_detail as fund_mod

logger = logging.getLogger("tools.finance.stock_fundamentals")


@tool
async def stock_fundamentals(symbol: str) -> str:
    """
    查询个股财务详情：估值快照 + 最近 4 期财务摘要。

    Args:
        symbol: 股票代码，6 位（000001）或带后缀（000001.SZ）。

    Returns:
        JSON 字符串：{
          "summary": {"name","industry","pe","pb","market_cap"},
          "abstract": [
            {"date":"YYYY-MM-DD","revenue":...,"revenue_yoy":...,"net_profit":...,
             "net_profit_yoy":...,"roe":...,"gross_margin":...},
            ... up to 4 entries
          ]
        }
    """
    try:
        data = await fund_mod.fetch_fundamentals_detail(symbol)
    except Exception as exc:
        logger.warning("stock_fundamentals 调用失败 symbol=%s err=%s", symbol, exc)
        return json.dumps({"error": f"获取失败：{exc}"}, ensure_ascii=False)

    return json.dumps({"symbol": symbol, **data}, ensure_ascii=False)
