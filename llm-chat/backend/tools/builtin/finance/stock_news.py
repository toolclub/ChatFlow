"""stock_news — 个股新闻 / 公告 / 研报"""
GUIDANCE = (
    "查一只 A 股最近的新闻 / 公告 / 研报标题列表（按时间倒序）。"
    "用户问「XX 最近为啥涨/跌」「XX 有什么消息面」「XX 利好/利空」「最近研报怎么看 XX」时召唤。"
    "返回结构化列表（标题+时间+来源+URL），请基于此判断情绪倾向，不要凭记忆。"
    "若需要原文细节再用 fetch_webpage 拉某条 URL；不要把全部 URL 都拉。"
)
ERROR_HINT = "若返回为空，说明 akshare 该接口当前不可用或个股无近期新闻；可换问 stock_quote 看价格变化。"
TAGS = ["finance", "research"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

from quant.data import news as news_mod

logger = logging.getLogger("tools.finance.stock_news")


@tool
async def stock_news(symbol: str, kind: str = "news", limit: int = 8) -> str:
    """
    查询个股新闻 / 公告 / 研报标题。

    Args:
        symbol: 股票代码，6 位（000001）或带后缀（000001.SZ）。
        kind:   数据类型 —— "news"(默认，新闻) | "notice"(公告) | "research"(研报)。
        limit:  返回条数，默认 8，最多 20。

    Returns:
        JSON 字符串：[{"date","title","source","url",...}]；无数据时返回 "[]"。
    """
    limit = max(1, min(int(limit), 20))
    kind_norm = (kind or "news").strip().lower()
    try:
        if kind_norm == "notice":
            items = await news_mod.fetch_stock_notices(symbol, limit=limit)
        elif kind_norm == "research":
            items = await news_mod.fetch_research_reports(symbol, limit=limit)
        else:
            items = await news_mod.fetch_stock_news(symbol, limit=limit)
    except Exception as exc:
        logger.warning("stock_news 调用失败 symbol=%s kind=%s err=%s", symbol, kind_norm, exc)
        return json.dumps({"error": f"获取失败：{exc}", "items": []}, ensure_ascii=False)

    return json.dumps({"symbol": symbol, "kind": kind_norm, "items": items}, ensure_ascii=False)
