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
from typing import AsyncGenerator, Any

from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

# 避免循环引用，仅在类型提示中使用
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from llm.providers import LLMProvider

logger = logging.getLogger("llm.client")


class LLMClient:
    """
    封装 AsyncOpenAI，绑定模型名和默认 temperature。

    通过 LLMProvider 注入厂商特有逻辑（如 DeepSeek 的 thinking 模式）。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        temperature: float = 0.7,
        provider: LLMProvider | None = None,
    ) -> None:
        """
        参数：
            client:      共享的 AsyncOpenAI HTTP 客户端
            model:       模型名称
            temperature: 默认温度，可在 ainvoke 调用时覆盖
            provider:    提供商抽象（DI 注入），用于获取 extra_body 等厂商参数
        """
        self._client = client
        self.model = model
        self.temperature = temperature
        self.provider = provider

    async def ainvoke(
        self,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None = None,
        temperature: float | None = None,
        timeout: float = 180.0,
        extra_body: dict | None = None,
    ) -> ChatCompletion:
        """
        异步调用 LLM，返回原始 ChatCompletion 对象。

        参数：
            messages:    OpenAI 格式消息列表
            tools:       OpenAI function calling schema 列表
            temperature: 覆盖实例默认温度
            timeout:     超时秒数
            extra_body:  覆盖 Provider 提供的默认参数
        """
        temp = temperature if temperature is not None else self.temperature
        # 优先使用显式传入的 extra_body，否则从 provider 获取
        eb = extra_body
        if eb is None and self.provider:
            eb = self.provider.get_extra_body(temp)

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temp,
        }
        if tools:
            create_kwargs["tools"] = tools
        if eb:
            create_kwargs["extra_body"] = eb

        logger.debug(
            "LLM 请求 | model=%s | messages=%d | tools=%s | temperature=%.2f",
            self.model, len(messages), len(tools) if tools else 0, temp,
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
        """流式调用 LLM。"""
        temp = temperature if temperature is not None else self.temperature
        eb = extra_body
        if eb is None and self.provider:
            eb = self.provider.get_extra_body(temp)

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
        if eb:
            create_kwargs["extra_body"] = eb

        stream: AsyncStream[ChatCompletionChunk] = await self._client.chat.completions.create(
            **create_kwargs
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
            elif hasattr(delta, "reasoning_content") and delta.reasoning_content:
                yield "\x00THINK\x00" + delta.reasoning_content

    async def astream_with_tools(
        self,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam],
        temperature: float | None = None,
        timeout: float = 180.0,
        extra_body: dict | None = None,
    ) -> tuple[str, str, list[dict]]:
        """流式调用 LLM（绑定工具）。"""
        temp = temperature if temperature is not None else self.temperature
        eb = extra_body
        if eb is None and self.provider:
            eb = self.provider.get_extra_body(temp)

        create_kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": temp,
            "stream": True,
            "timeout": timeout,
        }
        if eb:
            create_kwargs["extra_body"] = eb

        stream: AsyncStream[ChatCompletionChunk] = await self._client.chat.completions.create(
            **create_kwargs
        )

        import time as _time

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        # tool_calls 拼装：index → {id, name, arguments_fragments}
        tc_builders: dict[int, dict] = {}
        # 已通知前端"开始生成"的工具 index 集合
        tc_notified: set[int] = set()
        # 参数流式节流：攒一批再发（防止 SSE 洪泛）
        _args_buf: list[str] = []
        _args_last_flush = _time.monotonic()
        _ARGS_FLUSH_INTERVAL = 0.2  # 200ms 发一次
        _ARGS_FLUSH_SIZE = 500      # 或累积 500 字符发一次

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 文本内容
            if delta.content:
                content_parts.append(delta.content)
                yield ("content", delta.content)

            # 推理过程
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                thinking_parts.append(delta.reasoning_content)
                yield ("thinking", delta.reasoning_content)

            # tool_calls fragments
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_builders:
                        tc_builders[idx] = {
                            "id": tc_delta.id or "",
                            "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                            "arguments": "",
                        }
                    else:
                        if tc_delta.id:
                            tc_builders[idx]["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tc_builders[idx]["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        frag = tc_delta.function.arguments
                        tc_builders[idx]["arguments"] += frag
                        _args_buf.append(frag)
                        # 节流：200ms 或 500 字符发一批（防止 SSE 洪泛）
                        _now = _time.monotonic()
                        _buf_len = sum(len(s) for s in _args_buf)
                        if _now - _args_last_flush >= _ARGS_FLUSH_INTERVAL or _buf_len >= _ARGS_FLUSH_SIZE:
                            yield ("tool_call_args", "".join(_args_buf))
                            _args_buf.clear()
                            _args_last_flush = _now

                    # 首次检测到工具名时，立即通知前端
                    if idx not in tc_notified and tc_builders[idx]["name"]:
                        tc_notified.add(idx)
                        yield ("tool_call_start", tc_builders[idx]["name"])

        # 刷出剩余的参数缓冲
        if _args_buf:
            yield ("tool_call_args", "".join(_args_buf))
            _args_buf.clear()

        # 拼装最终结果
        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        tool_calls = [
            {"id": v["id"], "name": v["name"], "arguments": v["arguments"]}
            for _, v in sorted(tc_builders.items())
        ]

        # 通过特殊 yield 传递最终结果
        import json
        yield ("__done__", json.dumps({
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
        }, ensure_ascii=False))

    def __repr__(self) -> str:
        return f"LLMClient(model={self.model!r}, temperature={self.temperature})"
