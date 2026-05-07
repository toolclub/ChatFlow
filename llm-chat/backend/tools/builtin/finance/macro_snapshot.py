"""macro_snapshot — 中国宏观快照"""
GUIDANCE = (
    "拉一份中国宏观经济快照：CPI / PPI / M2 同比 + Shibor 隔夜利率 + USD/CNY 中间价。"
    "用户问「现在宏观怎么样」「是不是该买」「利率 / 通胀」「人民币贬值」时召唤。"
    "数据更新慢（月度为主）；同一天内多次召唤会命中缓存。"
)
ERROR_HINT = "宏观接口可能因 akshare 上游波动返回部分字段为空，缺哪条就忽略哪条。"
TAGS = ["finance", "macro"]
DISPLAY_MODE = "default"

import json
import logging

from langchain_core.tools import tool

from quant.data import macro as macro_mod

logger = logging.getLogger("tools.finance.macro_snapshot")


@tool
async def macro_snapshot() -> str:
    """
    查询当前中国宏观经济快照 —— 通胀 / 货币 / 利率 / 汇率。

    Returns:
        JSON 字符串：{
          "cn":   [{"name":"CPI 同比","value":...,"unit":"%","date":"YYYY-MM"}, ...],
          "rate": [{"name":"Shibor 隔夜","value":...,"unit":"%","date":"YYYY-MM-DD"}],
          "fx":   [{"name":"USD/CNY 中间价","value":...,"date":"YYYY-MM-DD"}]
        }
    """
    try:
        data = await macro_mod.fetch_macro_snapshot()
    except Exception as exc:
        logger.warning("macro_snapshot 调用失败 err=%s", exc)
        return json.dumps({"error": f"获取失败：{exc}"}, ensure_ascii=False)

    return json.dumps(data, ensure_ascii=False)
