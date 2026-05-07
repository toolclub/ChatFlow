"""industry_overview — A 股行业板块概览"""
GUIDANCE = (
    "拉一份 A 股申万一级行业当日涨跌排行（按涨跌幅降序），含成交额 + 领涨股。"
    "用户问「今天哪个板块涨」「行业轮动」「什么主题在风口」时召唤。"
    "返回前 N 个行业，请基于此说明哪些主题强势 / 弱势，不要凭记忆。"
)
ERROR_HINT = "板块接口偶发抖动；如返回空可换 macro_snapshot 看大盘氛围。"
TAGS = ["finance", "industry"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

from quant.data import industry as industry_mod

logger = logging.getLogger("tools.finance.industry_overview")


@tool
async def industry_overview(top_n: int = 20) -> str:
    """
    查询当日 A 股行业板块涨跌排行（申万一级）。

    Args:
        top_n: 返回多少个行业，默认 20，最多 50。

    Returns:
        JSON 字符串：{"items": [{"name","change_pct","amount","leader"}, ...]}
    """
    n = max(5, min(int(top_n), 50))
    try:
        items = await industry_mod.fetch_industry_overview(top_n=n)
    except Exception as exc:
        logger.warning("industry_overview 调用失败 err=%s", exc)
        return json.dumps({"error": f"获取失败：{exc}", "items": []}, ensure_ascii=False)

    return json.dumps({"items": items}, ensure_ascii=False)
