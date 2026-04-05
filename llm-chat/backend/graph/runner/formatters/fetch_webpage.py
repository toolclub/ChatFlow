"""
FetchWebpageFormatter：网页抓取结果格式化器

根据输出内容判断抓取是否成功（以特定前缀识别失败），
推送带状态的 tool_result 事件。
"""
from typing import AsyncGenerator, ClassVar

from graph.runner.formatters.base import ToolResultFormatter
from graph.runner.utils import sse


class FetchWebpageFormatter(ToolResultFormatter):
    """
    网页抓取结果格式化器。

    通过输出内容的前缀判断成功/失败，无需额外解析。
    """

    # 标识抓取失败的输出前缀
    _FAIL_PREFIXES: ClassVar[tuple[str, ...]] = ("读取超时", "HTTP 错误", "读取失败")

    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        """推送 tool_result 事件，带 done/fail 状态。"""
        status = "fail" if any(raw.startswith(p) for p in self._FAIL_PREFIXES) else "done"
        yield sse({"tool_result": {"name": name, "status": status}})
