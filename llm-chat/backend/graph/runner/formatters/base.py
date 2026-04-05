"""
ToolResultFormatter：工具结果格式化器抽象基类

职责：将工具执行结果格式化为 SSE 字符串流。
每种工具可以有不同的格式化策略（搜索结果、网页抓取、通用输出等）。

扩展方式：
  1. 继承 ToolResultFormatter
  2. 实现 format(name, raw) 方法
  3. 在 formatters/__init__.py 的 REGISTRY 中注册
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator


class ToolResultFormatter(ABC):
    """
    工具结果格式化器抽象基类。

    不同工具的输出格式差异很大（搜索结果列表、网页文本、计算结果等），
    策略模式允许为每个工具注册专属格式化逻辑，无需 if/else。
    """

    @abstractmethod
    async def format(self, name: str, raw: str) -> AsyncGenerator[str, None]:
        """
        格式化工具输出为 SSE 字符串。

        参数：
            name: 工具名称
            raw:  工具原始输出字符串

        Yields:
            SSE 格式字符串（"data: {...}\\n\\n"）
        """
        ...
