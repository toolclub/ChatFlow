"""
LangGraph 图节点定义

节点列表：
  retrieve_context  ── 从 ConversationStore + Qdrant 检索上下文，构建消息列表
  call_model        ── 调用 LLM（绑定工具），生成回复或工具调用指令
  save_response     ── 将用户消息和 AI 回复持久化到 ConversationStore
  compress_memory   ── 按需触发对话压缩（生成摘要 + 写入 Qdrant）

工厂函数（make_*）用于将运行时依赖（LLM、工具列表）注入到节点闭包中，
避免全局变量，方便测试和热重载。
"""
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from config import LONGTERM_MEMORY_ENABLED, ROUTER_MODEL, ROUTE_MODEL_MAP, SEARCH_MODEL
from llm.chat import get_chat_llm
from graph.state import GraphState
from memory import store as memory_store
from memory.compressor import maybe_compress
from memory.context_builder import build_messages

logger = logging.getLogger("graph.nodes")


# ── 节点 0：路由模型选择 ──────────────────────────────────────────────────────

ROUTE_PROMPT = """你是一个意图分类器。根据用户消息，输出以下四个标签之一，不要输出任何其他内容：

- chat         （普通聊天、解释通用概念、翻译、写作、数学、逻辑推理、创意建议、日常对话
                 ── 模型凭已有知识能准确回答的问题）

- code         （纯代码任务：根据明确需求直接编写/调试/重构代码，不需要先查询外部资料）

- search       （需要先联网查询再回答，但最终不是写代码：
                 1. 实时/最新信息：新闻、股价、天气、近期事件、最新版本
                 2. 具体事实核查：某技术/产品哪年出现、哪个公司提出、具体规格参数
                 3. 对近 3 年新技术/协议/框架没有把握的知识性问题）

- search_code  （需要先联网查询资料，再基于查询结果写代码：
                 例如：查官方文档/仓库/示例后写 demo、根据最新 API 写代码、
                 参考某框架的用法实现功能）

【判断原则】
- 明确要求"查官方/查文档/查仓库/参考官方"再写代码 → search_code
- 只是写代码，需求明确不需要查资料 → code
- 只是查信息，不需要写代码 → search
- 当不确定是 search 还是 search_code 时，优先选 search_code

只输出标签本身，例如：chat"""

async def route_model(state: GraphState) -> dict:
    user_msg = state["user_message"]
    llm = get_chat_llm(model=ROUTER_MODEL, temperature=0.0)

    resp = await llm.ainvoke([
        HumanMessage(content=f"{ROUTE_PROMPT}\n\n用户消息：{user_msg}")
    ])

    raw = resp.content.strip().lower()
    route = raw.split()[0] if raw.split() else "chat"
    if route not in ("code", "search", "chat", "search_code"):
        route = "chat"

    answer_model = ROUTE_MODEL_MAP.get(route, state["model"])

    # search / search_code 的第一阶段必须用支持工具调用的模型（SEARCH_MODEL）；
    # 第二阶段（call_model_after_tool）再切换到 answer_model（如代码模型）写最终答案。
    # code / chat 路由不调用工具，tool_model 与 answer_model 相同。
    needs_tools = route in ("search", "search_code")
    tool_model = SEARCH_MODEL if needs_tools else answer_model

    return {
        "route": route,
        "tool_model": tool_model,
        "answer_model": answer_model,
    }


# ── 节点 1：检索上下文 ────────────────────────────────────────────────────────

def make_retrieve_context(tool_names: list[str]):
    """
    工厂函数：创建 retrieve_context 节点。

    职责：
      1. 从 Qdrant 检索长期记忆
      2. 用余弦相似度判断是否触发忘记模式
      3. 调用 context_builder 组装历史消息 + 系统提示
    """
    async def retrieve_context(state: GraphState) -> dict:
        conv_id = state["conv_id"]
        user_msg = state["user_message"]
        conv = memory_store.get(conv_id)

        long_term: list[str] = []
        forget_mode = False

        if LONGTERM_MEMORY_ENABLED and user_msg:
            from rag import retriever as rag_retriever
            long_term = await rag_retriever.search_memories(conv_id, user_msg)

            if not long_term and conv:
                if conv.mid_term_summary:
                    relevant = await rag_retriever.is_relevant_to_summary(
                        user_msg, conv.mid_term_summary
                    )
                else:
                    # 无摘要时，与最近几条用户消息比较
                    recent = [m.content for m in conv.messages if m.role == "user"][-2:]
                    if recent:
                        relevant = await rag_retriever.is_relevant_to_recent(user_msg, recent)
                    else:
                        relevant = True
                forget_mode = not relevant

        # 构建历史消息列表（含系统提示、摘要、长期记忆、滑动窗口）
        history_messages = build_messages(conv, long_term, forget_mode, tool_names)
        # 追加本轮用户消息
        history_messages.append(HumanMessage(content=user_msg))

        return {
            "messages": history_messages,
            "long_term_memories": long_term,
            "forget_mode": forget_mode,
        }

    return retrieve_context


# ── 节点 2：调用 LLM ──────────────────────────────────────────────────────────

def make_call_model(tools: list[BaseTool]):
    """
    工厂函数：创建 call_model 节点。

    职责：
      - 从 state 读取 model 和 temperature，动态获取 LLM（已按 key 缓存）
      - 将 state.messages 送入 LLM
      - 若 LLM 返回工具调用 → should_continue 路由到 tools 节点
      - 若 LLM 返回最终回复 → 更新 full_response
    """
    async def call_model(state: GraphState) -> dict:
        route = state.get("route", "")
        model = state.get("tool_model") or state["model"]
        temperature = state["temperature"]
        llm = get_chat_llm(model=model, temperature=temperature)

        # search / search_code 路由绑定工具（tool_model 已是支持工具调用的模型）；
        # 未启用路由（route 为空）时也绑定，让模型自主决定是否调用；
        # code / chat 路由不绑定，避免误触发网络搜索。
        use_tools = tools and (not route or route in ("search", "search_code"))
        llm_with_tools = llm.bind_tools(tools) if use_tools else llm

        messages = list(state["messages"])
        response = await llm_with_tools.ainvoke(messages)

        content = response.content if isinstance(response.content, str) else ""
        return {
            "messages": [response],
            "full_response": content,
        }

    return call_model


# ── 节点 3：保存回复 ──────────────────────────────────────────────────────────

async def save_response(state: GraphState) -> dict:
    """
    将本轮用户消息和 AI 最终回复追加到 ConversationStore 并持久化。
    工具调用中间过程不写入（保持 conv.messages 只含 user/assistant 对）。
    """
    conv_id = state["conv_id"]
    user_msg = state["user_message"]
    full_response = state.get("full_response", "")

    memory_store.add_message(conv_id, "user", user_msg)
    if full_response:
        memory_store.add_message(conv_id, "assistant", full_response)

    return {}


# ── 节点 4：压缩记忆 ──────────────────────────────────────────────────────────

async def compress_memory(state: GraphState) -> dict:
    """
    按需触发对话压缩：
      - 对超过阈值的旧消息生成摘要
      - 同时将这批消息写入 Qdrant 长期记忆
    不影响流式输出（在 save_response 之后运行）。
    """
    conv_id = state["conv_id"]
    try:
        compressed = await maybe_compress(conv_id)
    except Exception as exc:
        logger.error("压缩失败 conv=%s: %s", conv_id, exc)
        compressed = False
    return {"compressed": compressed}

def make_call_model_after_tool(tools: list[BaseTool]):
    async def call_model_after_tool(state: GraphState) -> dict:
        # ✅ 用 answer_model
        model = state["answer_model"]
        temperature = state["temperature"]

        llm = get_chat_llm(model=model, temperature=temperature)

        # ⚠️ 工具后通常不需要再 bind_tools（避免死循环）
        messages = list(state["messages"])
        messages = messages[-6:]
        response = await llm.ainvoke(messages)

        content = response.content if isinstance(response.content, str) else ""

        return {
            "messages": [response],
            "full_response": content,
        }

    return call_model_after_tool

