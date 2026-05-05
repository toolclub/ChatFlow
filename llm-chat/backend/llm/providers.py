from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """
    LLM 提供商抽象类，用于封装不同厂商特有的参数和行为。
    """

    @abstractmethod
    def get_extra_body(self, temperature: float) -> dict[str, Any]:
        """返回传递给 OpenAI SDK 的 extra_body 参数。"""
        pass


class OpenAIProvider(LLMProvider):
    """通用 OpenAI 或兼容接口提供商。"""

    def get_extra_body(self, temperature: float) -> dict[str, Any]:
        return {}


class DeepSeekProvider(LLMProvider):
    """DeepSeek 提供商，支持 thinking 和 reasoning_effort。"""

    def get_extra_body(self, temperature: float) -> dict[str, Any]:
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }


class MiniMaxProvider(LLMProvider):
    """MiniMax 提供商。"""

    def get_extra_body(self, temperature: float) -> dict[str, Any]:
        # 目前 MiniMax 透传参数为空，未来可扩展
        return {}


def get_provider_for_model(model: str) -> LLMProvider:
    """根据模型名称路由到对应的提供商实例。"""
    m = model.lower()
    if "deepseek" in m:
        return DeepSeekProvider()
    if "minimax" in m:
        return MiniMaxProvider()
    return OpenAIProvider()
