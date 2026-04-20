"""
内置工具：日期时间查询
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = (
    "问系统时钟当前时刻——带时区的「此刻」。"
    "system prompt 里已经告诉你今天的日期；只在需要精确到时 / 分 / 秒，或需要某个非默认时区时才召唤它。"
)
ERROR_HINT = "时区格式应为 IANA 格式（如 Asia/Shanghai、America/New_York）。"
TAGS = ["utility"]
DISPLAY_MODE = "default"
from datetime import datetime

from langchain_core.tools import tool


@tool
def get_current_datetime(timezone: str = "Asia/Shanghai") -> str:
    """
    问系统时钟当前时刻——带时区的"此刻"。

    何时召唤：需要精确到时 / 分 / 秒，或需要某个非默认时区。
    何时不召唤：只是想知道今天日期——system prompt 里已经有了。

    Args:
        timezone: IANA 时区名，如 "Asia/Shanghai"（默认）、"UTC"、"America/New_York"

    Returns:
        格式化的当前日期时间字符串
    """
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekdays[now.weekday()]
        return (
            f"{now.year}年{now.month}月{now.day}日 {weekday} "
            f"{now.strftime('%H:%M:%S')} ({timezone})"
        )
    except Exception as exc:
        return f"获取时间失败: {exc}"
