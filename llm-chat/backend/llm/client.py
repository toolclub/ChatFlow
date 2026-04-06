"""
LLMClient：原生 AsyncOpenAI 客户端封装

职责单一：只负责 HTTP 请求，不做消息格式转换。

设计原则：
  - 接受 OpenAI dict 格式消息（{"role": "...", "content": "..."}）
  - 返回原始 ChatCompletion 对象，调用方自行解析
  - 绑定 model + temperature，避免调用方重复传入
  - 内置超时保护，防止 nginx 断流

使用方式：
    client = LLMClient(AsyncOpenAI(base_url=..., api_key=...), model="xxx", temperature=0.7)
    completion = await client.ainvoke([{"role": "user", "content": "hello"}])
    content = completion.choices[0].message.content
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

logger = logging.getLogger("llm.client")


class LLMClient:
    """
    封装 AsyncOpenAI，绑定模型名和默认 temperature。

    不依赖 LangChain，消息格式使用 OpenAI 原生 dict 列表。
    上层（节点）负责将 LangChain BaseMessage 转换为 dict，
    以及将 OpenAI tool_calls 转换回 LangChain AIMessage 格式（供 ToolNode 使用）。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        temperature: float = 0.7,
    ) -> None:
        """
        参数：
            client:      共享的 AsyncOpenAI HTTP 客户端
            model:       模型名称
            temperature: 默认温度，可在 ainvoke 调用时覆盖
        """
        self._client = client
        self.model = model
        self.temperature = temperature

    async def ainvoke(
        self,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None = None,
        temperature: float | None = None,
        timeout: float = 180.0,
    ) -> ChatCompletion:
        """
        异步调用 LLM，返回原始 ChatCompletion 对象。

        参数：
            messages:    OpenAI 格式消息列表，[{"role": "...", "content": "..."}]
            tools:       OpenAI function calling schema 列表，不传则不绑定工具
            temperature: 覆盖实例默认温度（不传则使用 self.temperature）
            timeout:     超时秒数，默认 180s

        返回：
            ChatCompletion：直接来自 openai SDK，调用方通过
            completion.choices[0].message.content / .tool_calls 读取结果
        """
        temp = temperature if temperature is not None else self.temperature

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temp,
        }
        if tools:
            create_kwargs["tools"] = tools

        logger.debug(
            "LLM 请求 | model=%s | messages=%d | tools=%s | temperature=%.2f",
            self.model,
            len(messages),
            len(tools) if tools else 0,
            temp,
        )

        completion: ChatCompletion = await asyncio.wait_for(
            self._client.chat.completions.create(**create_kwargs),
            timeout=timeout,
        )
        return completion

    async def astream(
        self,
        messages: list[ChatCompletionMessageParam],
        temperature: float | None = None,
        timeout: float = 180.0,
        extra_body: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        流式调用 LLM，逐 token yield 内容增量。

        仅用于无工具绑定的纯文本生成场景。
        工具调用（function calling）需使用 ainvoke，确保拿到完整 JSON。

        参数：
            messages:    OpenAI 格式消息列表
            temperature: 覆盖实例默认温度（不传则使用 self.temperature）
            timeout:     超时秒数（含首 chunk 等待时间），传给 openai SDK

        Yield：
            str：每个 token 的内容增量（空字符串不 yield）
        """
        temp = temperature if temperature is not None else self.temperature

        logger.debug(
            "LLM 流式请求 | model=%s | messages=%d | temperature=%.2f",
            self.model, len(messages), temp,
        )

        create_kwargs: dict = {
            "model":       self.model,
            "messages":    messages,
            "temperature": temp,
            "stream":      True,
            "timeout":     timeout,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body

        # 显式标注类型：stream=True 时返回 AsyncStream，类型系统无法自动收窄
        stream: AsyncStream[ChatCompletionChunk] = await self._client.chat.completions.create(
            **create_kwargs
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 普通内容 token
            if delta.content:
                yield delta.content
            # 智谱/DeepSeek 等模型的推理 token（thinking_content / reasoning_content）
            elif hasattr(delta, "reasoning_content") and delta.reasoning_content:
                yield "\x00THINK\x00" + delta.reasoning_content

    def __repr__(self) -> str:
        return f"LLMClient(model={self.model!r}, temperature={self.temperature})"
