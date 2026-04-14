"""
内置工具：日期时间查询
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = "获取当前精确日期和时间（支持时区）。系统提示中已有日期，仅在需要精确时间（时/分/秒）时才调用。"
ERROR_HINT = "时区格式应为 IANA 格式（如 Asia/Shanghai、America/New_York）。"
TAGS = ["utility"]
DISPLAY_MODE = "default"
from datetime import datetime

from langchain_core.tools import tool


@tool
def get_current_datetime(timezone: str = "Asia/Shanghai") -> str:
    """
    获取指定时区的当前日期和时间。

    Args:
        timezone: 时区名称，例如 "Asia/Shanghai"（默认）、"UTC"、"America/New_York"

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
