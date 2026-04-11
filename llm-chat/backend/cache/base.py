"""
语义缓存抽象层

SemanticCache      — 后端无关接口，具体实现在各 backend 子类
CacheLookupResult  — 命中时的返回结构
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CacheLookupResult:
    """缓存命中结果"""

    def __init__(self): ...

    answer: str
    matched_question: str
    similarity: float
    namespace: str


class SemanticCache(ABC):
    """
    语义缓存后端抽象接口。

    子类只需实现四个方法；调用方通过 cache.factory.get_cache() 拿到实例，
    无需关心底层存储。

    扩展新后端步骤：
      1. 继承 SemanticCache
      2. 实现 init / lookup / store / clear
      3. 在 cache/factory.py 的 init_cache() 中按配置选择实例化
    """

    @abstractmethod
    async def init(self) -> None:
        """应用启动时调用，完成索引/集合的创建或验证。"""

    @abstractmethod
    async def lookup(
            self,
            question: str,
            namespace: str = "global",
    ) -> Optional[CacheLookupResult]:
        """
        查询缓存。

        Args:
            question:  用户问题原文
            namespace: 缓存命名空间（隔离不同 system_prompt 或会话）

        Returns:
            命中 → CacheLookupResult；未命中 → None
        """

    @abstractmethod
    async def store(
            self,
            question: str,
            answer: str,
            namespace: str = "global",
            ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        写入缓存。

        Args:
            question:    用户问题原文
            answer:      LLM 最终回复
            namespace:   缓存命名空间
            ttl_seconds: 过期秒数，None 表示永不过期
        """

    @abstractmethod
    async def clear(self, namespace: Optional[str] = None) -> None:
        """
        清除缓存。

        Args:
            namespace: 指定命名空间则只清除该命名空间；为 None 则清除全部。
        """
