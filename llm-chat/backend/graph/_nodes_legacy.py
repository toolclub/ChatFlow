"""
LangGraph 图节点定义

节点列表：
  route_model         ── qwen3 判断意图，覆盖 state.model（可关闭）
  retrieve_context    ── 从 ConversationStore + Qdrant 检索上下文，构建消息列表
  planner             ── LLM 生成执行计划（search/search_code 路由触发）
  call_model          ── 调用 LLM（绑定工具），生成回复或工具调用指令
  call_model_after_tool ── 工具执行后，用 answer_model 生成最终回复
  reflector           ── 评估步骤执行结果，决定继续/完成/重试
  save_response       ── 将用户消息和 AI 回复持久化到 ConversationStore
  compress_memory     ── 按需触发对话压缩（生成摘要 + 写入 Qdrant）

工厂函数（make_*）用于将运行时依赖（LLM、工具列表）注入到节点闭包中，
避免全局变量，方便测试和热重载。
"""
import json
import logging
import asyncio
from datetime import date

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langchain_core.tools import BaseTool

from config import (
    LONGTERM_MEMORY_ENABLED,
    ROUTER_MODEL,
    ROUTE_MODEL_MAP,
    SEARCH_MODEL,
    SEMANTIC_CACHE_NAMESPACE_MODE,
    SEMANTIC_CACHE_SEARCH_TTL_HOURS,
    DEFAULT_SYSTEM_PROMPT,
    VISION_MODEL,
    VISION_BASE_URL,
    VISION_API_KEY,
)
from llm.chat import get_chat_llm
from graph.state import GraphState, PlanStep
from graph.event_types import (
    CacheHitNodeOutput,
    RouteNodeOutput,
    PlannerNodeOutput,
    ReflectorNodeOutput,
    CompressNodeOutput,
)
from memory import store as memory_store
from memory.compressor import maybe_compress
from memory.context_builder import build_messages

logger = logging.getLogger("graph.nodes")


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _to_openai_messages(messages) -> list[dict]:
    """
    将 LangChain BaseMessage 列表转为 OpenAI SDK 原生格式。
    HumanMessage.content 若已是 list（多模态），直接透传给 SDK，不经 LangChain 序列化。
    AIMessage 中的 tool_calls 同步转换为 OpenAI function calling 格式，
    确保 call_model_after_tool 在含图片场景下能正确重放工具调用历史。
    """
    role_map = {
        "SystemMessage":   "system",
        "HumanMessage":    "user",
        "AIMessage":       "assistant",
        "ToolMessage":     "tool",
        "FunctionMessage": "function",
    }
    result = []
    for msg in messages:
        role = role_map.get(type(msg).__name__, "user")
        entry: dict = {"role": role, "content": msg.content}
        if role == "tool":
            entry["tool_call_id"] = getattr(msg, "tool_call_id", "")
        if role == "assistant":
            lc_tool_calls = getattr(msg, "tool_calls", None) or []
            if lc_tool_calls:
                openai_tool_calls = []
                for tc in lc_tool_calls:
                    if isinstance(tc, dict):
                        tc_id   = tc.get("id", "")
                        tc_name = tc.get("name", "")
                        tc_args = tc.get("args", {})
                    else:
                        tc_id   = getattr(tc, "id", "")
                        tc_name = getattr(tc, "name", "")
                        tc_args = getattr(tc, "args", {})
                    openai_tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": tc_name,
                            "arguments": json.dumps(tc_args, ensure_ascii=False),
                        },
                    })
                entry["tool_calls"] = openai_tool_calls
                # OpenAI 规范：有 tool_calls 时 content 须为 None 或空字符串
                if not entry["content"]:
                    entry["content"] = None
        result.append(entry)
    return result


def _tools_to_openai_schema(tools: list) -> list[dict]:
    """
    将 LangChain BaseTool 列表转为 OpenAI tools 参数格式。
    用于含图片时直接调用 OpenAI SDK（绕过 LangChain bind_tools 序列化）。
    """
    result = []
    for tool in tools:
        # LangChain BaseTool 提供 args_schema（Pydantic model）
        schema = getattr(tool, "args_schema", None)
        if schema is not None:
            parameters = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema.schema()
            # 移除 pydantic 生成的 title 字段，保持简洁
            parameters.pop("title", None)
        else:
            parameters = {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": getattr(tool, "description", ""),
                "parameters": parameters,
            },
        })
    return result


# ── 节点 -1：语义缓存检查（图的第一个节点） ──────────────────────────────────

def _derive_cache_namespace(conv, mode: str, client_id: str = "") -> str:
    """
    根据命名空间模式派生缓存 namespace 字符串。

    "user"   → client_id，每个用户（浏览器）独立，多人系统推荐
    "conv"   → conv_id，每个对话独立（最细粒度，无跨会话复用）
    "prompt" → md5(system_prompt)[:8]，同 prompt 跨用户共享
    "global" → "global"，所有用户完全共享
    """
    import hashlib
    if mode == "user":
        return f"u:{client_id}" if client_id else "u:anon"
    if mode == "conv":
        return conv.id if conv else "global"
    if mode == "global":
        return "global"
    # 默认 "prompt" 模式
    prompt = (conv.system_prompt if conv else "") or DEFAULT_SYSTEM_PROMPT
    return hashlib.md5(prompt.encode()).hexdigest()[:8]


async def semantic_cache_check(state: GraphState) -> CacheHitNodeOutput:
    """
    语义缓存检查节点，作为图的最前置节点执行。

    命中：设置 full_response + cache_hit=True，后续 cache_routing 直跳 save_response。
    未命中：cache_hit=False，继续正常流程（route_model / retrieve_context）。
    """
    from cache.factory import get_cache
    from logging_config import get_conv_logger

    user_msg  = state["user_message"]
    conv_id   = state["conv_id"]
    client_id = state.get("client_id", "")
    clog      = get_conv_logger(client_id, conv_id)

    # 含图片的请求跳过缓存（图片内容不参与语义匹配）
    if state.get("images"):
        clog.info("Cache SKIP  | 含图片请求，跳过语义缓存 | user_msg='%.60s'", user_msg)
        return {"cache_hit": False, "full_response": "", "cache_similarity": 0.0}

    conv      = memory_store.get(conv_id)
    namespace = _derive_cache_namespace(conv, SEMANTIC_CACHE_NAMESPACE_MODE, client_id)
    cache     = get_cache()
    result    = await cache.lookup(user_msg, namespace)

    if result is None:
        clog.info(
            "Cache MISS  | ns=%s | 未命中，继续正常流程 | user_msg='%.60s'",
            namespace, user_msg,
        )
        return {"cache_hit": False, "full_response": "", "cache_similarity": 0.0}

    clog.info(
        "Cache HIT   | similarity=%.4f | ns=%s | matched='%.60s' | user_msg='%.60s'",
        result.similarity, namespace, result.matched_question, user_msg,
    )
    return {
        "cache_hit":        True,
        "full_response":    result.answer,
        "cache_similarity": result.similarity,
    }


# ── 节点 0：路由模型选择 ──────────────────────────────────────────────────────

ROUTE_PROMPT = """你是一个智能路由器。分析用户消息（和附带图片说明），选择最合适的处理方式，输出以下标签之一：

- chat         直接回答，无需联网或工具：
                 日常对话、解释概念、翻译、写作、数学、逻辑推理、分析图片内容

- code         纯代码任务，需求明确、无需查资料：
                 编写/调试/重构/解释代码，依据图片内容直接生成代码

- search       需联网查询，不涉及写代码：
                 实时/最新信息（新闻、股价、天气、版本号）、具体事实核查、
                 查询图片中出现的商品/地点/人物/文字的详细信息

- search_code  需先查资料再写代码：
                 查官方文档/API 后写代码、根据图片内容查资料再实现功能

【判断原则】
- 图片纯分析（描述/解读/OCR/情感）→ chat
- 图片内容需要联网核实或延伸查询 → search
- 根据图片直接写代码且需求明确 → code
- 根据图片写代码但需先查资料 → search_code
- 明确要求查官方/文档后写代码 → search_code
- 只写代码需求明确不查资料 → code
- 只查信息不写代码 → search
- 不确定 search 还是 search_code → 优先 search_code

只输出标签本身，例如：chat"""


async def route_model(state: GraphState) -> RouteNodeOutput:
    user_msg = state["user_message"]
    has_images = bool(state.get("images"))

    llm = get_chat_llm(model=ROUTER_MODEL, temperature=0.0)

    # 有图片时在路由提示中注入图片上下文，帮助模型基于意图做出正确路由
    if has_images:
        n = len(state["images"])
        routing_input = f"[用户附带了 {n} 张图片]\n用户消息：{user_msg}"
    else:
        routing_input = f"用户消息：{user_msg}"

    resp = await llm.ainvoke([
        HumanMessage(content=f"{ROUTE_PROMPT}\n\n{routing_input}")
    ])

    raw = resp.content.strip().lower()
    # search_code 必须在 search 之前检查，避免被 search 部分匹配
    route = "chat"
    for candidate in ("search_code", "search", "code", "chat"):
        if candidate in raw:
            route = candidate
            break

    # ── 模型选择 ────────────────────────────────────────────────────────────
    if has_images:
        # 有图片时：整条链路都需要视觉能力
        #   - tool_model：初始 call_model 需要理解图片内容才能决定调用哪些工具
        #   - answer_model：综合工具结果 + 图片时同样需要视觉
        vision = VISION_MODEL or ROUTE_MODEL_MAP.get("chat", state["model"])
        tool_model   = vision
        answer_model = vision
    else:
        answer_model = ROUTE_MODEL_MAP.get(route, state["model"])
        needs_tools  = route in ("search", "search_code")
        tool_model   = SEARCH_MODEL if needs_tools else answer_model

    from logging_config import get_conv_logger
    get_conv_logger(state.get("client_id", ""), state.get("conv_id", "")).info(
        "路由决策 | route=%s | has_images=%s | tool_model=%s | answer_model=%s | user_msg=%.60s",
        route, has_images, tool_model, answer_model, user_msg,
    )

    return {
        "route":        route,
        "tool_model":   tool_model,
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
                    recent = [m.content for m in conv.messages if m.role == "user"][-2:]
                    if recent:
                        relevant = await rag_retriever.is_relevant_to_recent(user_msg, recent)
                    else:
                        relevant = True
                forget_mode = not relevant

        history_messages = build_messages(conv, long_term, forget_mode, tool_names)

        # 构建用户消息：有图片时使用多模态格式
        images = state.get("images", [])
        if images:
            multimodal_content: list = []
            for img in images:
                url = img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                logger.info(
                    "retrieve_context 图片URL | conv=%s | prefix=%.40s | len=%d",
                    conv_id, url[:40], len(url),
                )
                multimodal_content.append({"type": "image_url", "image_url": {"url": url}})
            if user_msg:
                multimodal_content.append({"type": "text", "text": user_msg})
            history_messages.append(HumanMessage(content=multimodal_content))
        else:
            history_messages.append(HumanMessage(content=user_msg))

        return {
            "messages": history_messages,
            "long_term_memories": long_term,
            "forget_mode": forget_mode,
            # 初始化认知规划字段
            "plan": [],
            "current_step_index": 0,
            "step_iterations": 0,
            "reflector_decision": "",
            "reflection": "",
        }

    return retrieve_context


# ── 节点 1.5：任务规划器 ──────────────────────────────────────────────────────
today = date.today().strftime("%Y年%m月%d日")
PLANNER_SYSTEM = f"""你是一个任务规划专家。分析用户的请求，制定清晰的执行计划。
当前日期：{today}。搜索时直接用核心关键词，不要手动添加年份。
要求：
- 将任务分解为若干个具体可执行的步骤 当前日期：{today}
- 每步有明确的操作（搜索信息、获取数据、计算、分析、撰写等） 当前日期：{today}
- 步骤间有逻辑顺序，后一步依赖前一步的结果 当前日期：{today}
- 如果任务很简单只需一步操作，就只列 1 个步骤 当前日期：{today}

输出格式（JSON）：
{{"steps": [{{"title": "简短标题（10字以内）", "description": "具体描述（说明要做什么、搜索什么）"}}]}}

只输出 JSON，不要任何解释。"""

def make_planner():
    """工厂函数：创建 planner 节点（任务规划）"""

    async def planner(state: GraphState) -> PlannerNodeOutput:
        route = state.get("route", "")

        # 只对搜索类任务或无路由模式进行规划
        needs_planning = not route or route in ("search", "search_code")
        if not needs_planning:
            return {
                "plan": [],
                "current_step_index": 0,
                "step_iterations": 0,
            }

        user_msg = state["user_message"]
        images = state.get("images", [])

        # ── 有图片时：先用视觉模型把图"翻译"成文字，再交给规划模型 ────────────
        # 视觉模型（Ollama 本地）只在 VISION_BASE_URL 上存在，不能用 get_chat_llm。
        # 规划模型（主接口）不需要看图，只需要读文字描述即可制定步骤。
        image_description = ""
        if images:
            try:
                from openai import AsyncOpenAI
                vision_model = VISION_MODEL
                if vision_model:
                    vision_client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
                    vision_content: list = []
                    for img in images:
                        url = img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                        vision_content.append({"type": "image_url", "image_url": {"url": url}})
                    vision_content.append({
                        "type": "text",
                        "text": (
                            "请仔细观察图片，用中文详细描述你看到的内容，"
                            "重点描述：错误信息、代码片段、界面异常、文字内容、关键数据等。"
                            "描述要具体，方便后续推理分析。"
                        ),
                    })
                    vision_resp = await asyncio.wait_for(
                        vision_client.chat.completions.create(
                            model=vision_model,
                            messages=[{"role": "user", "content": vision_content}],
                            temperature=0.1,
                        ),
                        timeout=60,
                    )
                    image_description = vision_resp.choices[0].message.content or ""
                    logger.info(
                        "Planner 视觉预处理完成 | vision_model=%s | desc_len=%d | preview='%.200s'",
                        vision_model, len(image_description), image_description,
                    )
            except Exception as exc:
                logger.warning("Planner 视觉预处理失败，退化为纯文字规划 | error=%s", exc)

        # 规划模型：用主接口上存在的模型
        model = state.get("tool_model") or state["model"]
        if model == (VISION_MODEL or "") or not model:
            model = SEARCH_MODEL or state["model"]

        # streaming=False + 重试：MiniMax 等厂商偶发返回空 content
        llm = get_chat_llm(model=model, temperature=0.1)

        # 组装规划输入：有图片时把视觉描述嵌入，让规划模型知道图里有什么
        if images:
            if image_description:
                planning_msg = (
                    f"[图片内容分析]\n{image_description}\n\n"
                    f"[用户请求]\n{user_msg}"
                )
            else:
                planning_msg = (
                    f"[用户附带了 {len(images)} 张图片，内容无法解析]\n"
                    f"用户请求：{user_msg}"
                )
        else:
            planning_msg = user_msg

        messages = [
            SystemMessage(content=PLANNER_SYSTEM),
            HumanMessage(content=planning_msg),
        ]

        # ── 层1：重试拿到非空 content ──────────────────────────────────────────
        content = ""
        for attempt in range(3):
            response = await llm.ainvoke(messages)
            raw = response.content if isinstance(response.content, str) else ""
            content = raw.strip()
            logger.info(
                "Planner 原始响应 [第%d次] | model=%s | len=%d | tool_calls=%s | raw='%.200s'",
                attempt + 1,
                model,
                len(raw),
                getattr(response, "tool_calls", None) or [],
                raw,
            )
            if content:
                logger.info(
                    "Planner 获得响应 [第%d次] | model=%s | len=%d",
                    attempt + 1, model, len(content),
                )
                break
            logger.warning(
                "Planner 返回空内容 [第%d/3次] | model=%s | response_id=%s | tool_calls=%s",
                attempt + 1,
                model,
                getattr(response, "id", "unknown"),
                getattr(response, "tool_calls", None) or [],
            )

        # ── 层2：从响应中提取 JSON ──────────────────────────────────────────────
        plan_steps: list[PlanStep] = []
        try:
            if not content:
                raise ValueError("三次重试后仍返回空内容")

            # 去除 markdown code block
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        content = part
                        break

            # 按 { } 定位 JSON 对象（兼容前后有说明文字）
            start = content.find("{")
            end   = content.rfind("}") + 1
            if start != -1 and end > start:
                content = content[start:end]

            logger.info(
                "Planner JSON 提取后 | extracted='%.300s'", content
            )

            data = json.loads(content)
            for i, s in enumerate(data.get("steps", [])):
                plan_steps.append(PlanStep(
                    id=str(i + 1),
                    title=s.get("title", f"步骤 {i + 1}"),
                    description=s.get("description", ""),
                    status="pending",
                    result="",
                ))
            # 硬性限制最多 3 步，防止步骤过多导致总耗时过长触发 nginx 超时
            plan_steps = plan_steps[:3]
            logger.info(
                "Planner 解析成功 | steps=%d | titles=%s",
                len(plan_steps),
                [s["title"] for s in plan_steps],
            )

        # ── 层3：兜底单步 ───────────────────────────────────────────────────────
        except Exception as e:
            logger.warning(
                "Planner JSON 解析失败，使用单步兜底 | error=%s | model=%s | content='%.300s'",
                e, model, content,
            )
            plan_steps = [PlanStep(
                id="1",
                title="执行任务",
                description=user_msg,
                status="pending",
                result="",
            )]

        # 第一步标记为 running
        if plan_steps:
            plan_steps[0] = {**plan_steps[0], "status": "running"}

        return {
            "plan": plan_steps,
            "current_step_index": 0,
            "step_iterations": 0,
        }

    return planner


# ── 节点 2：调用 LLM ──────────────────────────────────────────────────────────

def make_call_model(tools: list[BaseTool]):
    """
    工厂函数：创建 call_model 节点。

    职责：
      - 从 state 读取 model 和 temperature，动态获取 LLM（已按 key 缓存）
      - 若有执行计划，在本地消息副本中注入当前步骤上下文
      - 将 state.messages 送入 LLM
      - 若 LLM 返回工具调用 → should_continue 路由到 tools 节点
      - 若 LLM 返回最终回复 → 更新 full_response
    """
    async def call_model(state: GraphState) -> dict:
        route = state.get("route", "")
        model = state.get("tool_model") or state["model"]
        temperature = state["temperature"]
        conv_id = state.get("conv_id", "")
        llm = get_chat_llm(model=model, temperature=temperature)

        use_tools = bool(tools and (not route or route in ("search", "search_code")))
        # 工具调用路由强制 streaming=False：MiniMax 在 streaming+bind_tools 模式下
        # 会在内容生成途中切换输出 tool_call 格式，导致 response.content 被截断。
        # chat/code 路由无工具绑定，streaming=True 正常流式输出。
        # search 路由无工具调用时内容由 CallModelEndHandler 从 on_chain_end 补发。
        llm = get_chat_llm(model=model, temperature=temperature)
        llm_with_tools = llm.bind_tools(tools) if use_tools else llm

        messages = list(state["messages"])

        # 若有执行计划且处于初始调用（第一步），注入步骤上下文
        plan = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)
        step_iters = state.get("step_iterations", 0)

        logger.info(
            "call_model 开始 | conv=%s | model=%s | streaming=%s | use_tools=%s | "
            "step=%s/%s | iter=%s | messages=%d",
            conv_id, model, not use_tools, use_tools,
            current_idx + 1 if plan else "-", len(plan) if plan else "-",
            step_iters, len(messages),
        )

        if plan and current_idx < len(plan) and current_idx == 0 and step_iters == 0:
            # 仅首次调用时注入，后续步骤由 reflector 通过 messages 传递步骤指令
            step = plan[current_idx]
            total = len(plan)
            step_ctx = (
                f"\n\n---\n**[执行步骤 {current_idx + 1}/{total}]: {step['title']}**\n"
                f"具体任务：{step['description']}\n"
                "请使用工具完成此步骤，收集所需信息。"
            )
            # 将步骤上下文追加到最后的 HumanMessage（仅用于本次 LLM 调用，不写回 state）
            if messages and messages[-1].__class__.__name__ == 'HumanMessage':
                last_content = messages[-1].content
                if isinstance(last_content, list):
                    # 多模态消息：追加文本部分
                    messages[-1] = HumanMessage(
                        content=list(last_content) + [{"type": "text", "text": step_ctx}]
                    )
                else:
                    messages[-1] = HumanMessage(content=str(last_content) + step_ctx)

        # ── 含图片时绕过 LangChain，直接用 OpenAI SDK 发送多模态请求 ──────────────
        # LangChain ChatOpenAI 在序列化 HumanMessage list content 时存在兼容性问题，
        # 导致图片内容未被正确传递。OpenAI SDK 原生支持 list content，直接传入即可。
        # 无论是否需要工具（use_tools），含图片的请求统一走此分支：
        #   - 无工具：直接生成回复
        #   - 有工具：透传 tools schema，并将 SDK 返回的 tool_calls 转回 LangChain 格式
        if state.get("images"):
            from openai import AsyncOpenAI

            vision_model = VISION_MODEL or model
            openai_messages = _to_openai_messages(messages)
            client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)

            create_kwargs: dict = dict(
                model=vision_model,
                messages=openai_messages,
                temperature=temperature,
            )
            if use_tools and tools:
                create_kwargs["tools"] = _tools_to_openai_schema(tools)

            logger.info(
                "call_model (vision/direct) 请求发出 | conv=%s | model=%s | vision_model=%s"
                " | use_tools=%s | msgs=%d",
                conv_id, model, vision_model, use_tools, len(openai_messages),
            )
            try:
                oai_resp = await asyncio.wait_for(
                    client.chat.completions.create(**create_kwargs),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.error("call_model (vision) 超时 | conv=%s | model=%s", conv_id, model)
                raise
            except Exception as exc:
                logger.error("call_model (vision) 异常 | conv=%s | model=%s | error=%s",
                             conv_id, model, exc, exc_info=True)
                raise

            oai_msg = oai_resp.choices[0].message
            content  = oai_msg.content or ""
            oai_tool_calls = oai_msg.tool_calls or []

            if oai_tool_calls:
                # 将 OpenAI tool_calls 转回 LangChain AIMessage 格式，让 should_continue 正常路由
                lc_tool_calls = []
                for tc in oai_tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {"_raw": tc.function.arguments}
                    lc_tool_calls.append({
                        "id":   tc.id,
                        "name": tc.function.name,
                        "args": args,
                        "type": "tool_call",
                    })
                    logger.info(
                        "call_model (vision) tool_call | conv=%s | name=%s | args=%.200s",
                        conv_id, tc.function.name, tc.function.arguments,
                    )
                ai_msg = AIMessage(content=content, tool_calls=lc_tool_calls)
                logger.info(
                    "call_model (vision/direct) 完成(tool_calls) | conv=%s | model=%s"
                    " | tool_calls=%d | content_len=%d",
                    conv_id, model, len(lc_tool_calls), len(content),
                )
                return {"messages": [ai_msg], "full_response": content}

            logger.info(
                "call_model (vision/direct) 完成 | conv=%s | model=%s | content_len=%d | preview='%.100s'",
                conv_id, model, len(content), content,
            )
            return {"messages": [AIMessage(content=content)], "full_response": content}

        try:
            logger.info("call_model LLM请求发出 | conv=%s | model=%s", conv_id, model)
            response = await asyncio.wait_for(
                llm_with_tools.ainvoke(messages),
                timeout=180,
            )
        except asyncio.TimeoutError:
            logger.error("call_model 超时（180s） | conv=%s | model=%s", conv_id, model)
            raise
        except Exception as exc:
            logger.error("call_model LLM调用异常 | conv=%s | model=%s | error=%s", conv_id, model, exc, exc_info=True)
            raise

        content = response.content if isinstance(response.content, str) else ""
        tool_calls = getattr(response, "tool_calls", None) or []
        logger.info(
            "call_model 完成 | conv=%s | model=%s | tool_calls=%d | content_len=%d | content_preview='%.100s'",
            conv_id, model, len(tool_calls), len(content), content,
        )
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                logger.info("call_model tool_call | conv=%s | name=%s | args=%.200s", conv_id, name, str(args))
        return {
            "messages": [response],
            "full_response": content,
        }

    return call_model


# ── 节点 3：工具后 LLM 调用 ───────────────────────────────────────────────────

def make_call_model_after_tool(tools: list[BaseTool]):
    async def call_model_after_tool(state: GraphState) -> dict:
        model = state["answer_model"]
        temperature = state["temperature"]
        conv_id = state.get("conv_id", "")
        plan = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)

        llm = get_chat_llm(model=model, temperature=temperature)

        messages = list(state["messages"])
        # 保留最近 20 条消息（多步执行场景需要早期工具结果）
        messages = messages[-20:]

        # ── 计划模式：注入步骤边界指令 ──────────────────────────────────────
        # 目的：防止模型越界回答后续步骤，防止无限调用工具
        if plan and current_idx < len(plan):
            step = plan[current_idx]
            total = len(plan)
            tool_count = sum(1 for m in messages if type(m).__name__ == "ToolMessage")
            boundary = (
                f"\n\n===当前执行步骤===\n"
                f"步骤 {current_idx + 1}/{total}：{step['title']}\n"
                f"任务：{step['description']}\n"
                f"本步已调用工具 {tool_count} 次。"
            )
            if tool_count >= 2:
                boundary += (
                    "已有足够工具结果，请直接给出本步骤的结论，"
                    "不要继续调用工具，不要回答后续步骤的内容。"
                )
            # 追加到已有 SystemMessage 末尾（保留原始上下文）
            if messages and type(messages[0]).__name__ == "SystemMessage":
                messages = [SystemMessage(content=messages[0].content + boundary)] + messages[1:]

        logger.info(
            "call_model_after_tool 开始 | conv=%s | model=%s | step=%s/%s | messages=%d",
            conv_id, model,
            current_idx + 1 if plan else "-", len(plan) if plan else "-",
            len(messages),
        )

        # ── 含图片时同样绕过 LangChain，直接用 OpenAI SDK ──────────────────────
        # call_model_after_tool 的 messages 列表中含有多模态 HumanMessage（原始截图），
        # LangChain 同样无法正确序列化，须走 OpenAI SDK 原生路径。
        # 此节点只生成最终回复，无需再绑定工具（boundary 注入已限制工具调用）。
        if state.get("images"):
            from openai import AsyncOpenAI

            vision_model = VISION_MODEL or model
            openai_messages = _to_openai_messages(messages)
            client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
            logger.info(
                "call_model_after_tool (vision/direct) 请求发出 | conv=%s | model=%s | msgs=%d",
                conv_id, vision_model, len(openai_messages),
            )
            try:
                oai_resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=vision_model,
                        messages=openai_messages,
                        temperature=temperature,
                    ),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "call_model_after_tool (vision) 超时 | conv=%s | model=%s", conv_id, model
                )
                raise
            except Exception as exc:
                err_str = str(exc)
                if "1027" in err_str or "sensitive" in err_str.lower():
                    logger.warning("call_model_after_tool (vision) 触发内容审核 | conv=%s", conv_id)
                    return {
                        "messages": [],
                        "full_response": "抱歉，该内容触发了模型安全审核，无法生成回复。请换个方式描述问题。",
                    }
                logger.error(
                    "call_model_after_tool (vision) 异常 | conv=%s | model=%s | error=%s",
                    conv_id, model, exc, exc_info=True,
                )
                raise

            full_content = oai_resp.choices[0].message.content or ""
            logger.info(
                "call_model_after_tool (vision/direct) 完成 | conv=%s | model=%s"
                " | content_len=%d | preview='%.100s'",
                conv_id, vision_model, len(full_content), full_content,
            )
            return {
                "messages": [AIMessage(content=full_content)],
                "full_response": full_content,
            }

        try:
            logger.info(
                "call_model_after_tool LLM请求 | conv=%s | model=%s | streaming=True",
                conv_id, model,
            )
            response = await asyncio.wait_for(
                llm.ainvoke(messages),
                timeout=180,
            )
        except asyncio.TimeoutError:
            logger.error("call_model_after_tool 超时（180s） | conv=%s | model=%s", conv_id, model)
            raise
        except Exception as exc:
            err_str = str(exc)
            if "1027" in err_str or "sensitive" in err_str.lower():
                logger.warning("call_model_after_tool 触发内容审核 | conv=%s", conv_id)
                return {
                    "messages": [],
                    "full_response": "抱歉，该内容触发了模型安全审核，无法生成回复。请换个方式描述问题。",
                }
            logger.error(
                "call_model_after_tool LLM调用异常 | conv=%s | model=%s | error=%s",
                conv_id, model, exc, exc_info=True,
            )
            raise

        full_content = response.content if isinstance(response.content, str) else ""
        tool_calls = getattr(response, "tool_calls", None) or []
        logger.info(
            "call_model_after_tool 完成 | conv=%s | model=%s | "
            "tool_calls=%d | content_len=%d | content_preview='%.100s'",
            conv_id, model, len(tool_calls), len(full_content), full_content,
        )

        return {
            "messages": [response],
            "full_response": full_content,
        }

    return call_model_after_tool


# ── 节点 4：任务反思器 ────────────────────────────────────────────────────────

REFLECTOR_SYSTEM = """你是一个任务完成情况评估专家。

根据执行计划和当前步骤的结果，决定下一步行动：
- "done":     所有需要的信息已收集完毕，可以生成最终答案了
- "continue": 当前步骤完成，继续执行下一个步骤
- "retry":    当前步骤明确失败（工具报错），需要重试

规则（优先顺序）：
1. 这是最后一步且有任何结果 → done
2. 当前步骤有工具结果，且还有后续步骤 → continue
3. 工具明确报错（读取超时/HTTP错误/无结果）→ retry（最多2次）
4. 其他情况 → done（宁可有不完美答案，也不要无限循环）

输出格式（JSON）：
{"decision": "done|continue|retry", "reflection": "一句话评估"}

只输出 JSON。"""


def make_reflector():
    """工厂函数：创建 reflector 节点（任务反思与路由决策）"""

    async def reflector(state: GraphState) -> ReflectorNodeOutput:
        plan = state.get("plan", [])

        # 无计划时直接完成
        if not plan:
            return {"reflector_decision": "done", "reflection": "任务完成"}

        current_idx = state.get("current_step_index", 0)
        step_iters = state.get("step_iterations", 0)
        total = len(plan)
        full_response = state.get("full_response", "")

        # 安全边界：超出步骤范围或超过重试次数，强制完成（每步最多 3 次重试）
        if current_idx >= total or step_iters >= 3:
            updated_plan = _mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection": "步骤执行完成（达到边界条件）",
                "plan": updated_plan,
            }

        # 最后一步且有响应：直接完成
        is_last = current_idx >= total - 1
        if is_last and full_response:
            updated_plan = _mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection": "最后步骤执行完成",
                "plan": updated_plan,
            }

        # 提取最近消息用于评估
        messages = list(state.get("messages", []))
        recent = messages[-5:] if len(messages) > 5 else messages

        # 快速路径：非最后步骤 + 有工具调用结果 + 首次执行（未重试）
        # → 不调 LLM，直接 continue。
        # 原因：call_model_after_tool 的输出常包含"下一步行动"等措辞，
        # 会误导 LLM 认为后续步骤已处理，导致提前 done。
        if not is_last and step_iters == 0:
            has_tool_result = any(
                type(m).__name__ == "ToolMessage"
                for m in recent
            )
            if has_tool_result:
                updated_plan = _mark_step(plan, current_idx, "done")
                next_idx = current_idx + 1
                updated_plan = _mark_step(updated_plan, next_idx, "running")
                next_step = updated_plan[next_idx]
                step_msg = HumanMessage(
                    content=(
                        f"步骤 {current_idx + 1} 已完成。\n\n"
                        f"**[执行步骤 {next_idx + 1}/{total}]: {next_step['title']}**\n"
                        f"具体任务：{next_step['description']}\n"
                        "请完成此步骤。若需要新信息则使用工具；若已有足够上下文，直接给出结论。"
                    )
                )
                return {
                    "reflector_decision": "continue",
                    "reflection": f"步骤 {current_idx + 1} 工具调用完成，继续执行步骤 {next_idx + 1}",
                    "plan": updated_plan,
                    "messages": [step_msg],
                    "current_step_index": next_idx,
                    "step_iterations": 0,
                }

        recent_text = "\n".join([
            f"[{type(m).__name__}]: {str(m.content)[:600]}"
            for m in recent
        ])

        current_step = plan[current_idx]
        model = state.get("answer_model") or state.get("model", "")
        llm = get_chat_llm(model=model, temperature=0.1)

        eval_prompt = (
            f"执行计划共 {total} 步，当前步骤 {current_idx + 1}：{current_step['title']}\n"
            f"步骤描述：{current_step['description']}\n\n"
            f"最近执行记录：\n{recent_text}\n\n"
            f"是否还有后续步骤：{'是' if not is_last else '否（这是最后一步）'}"
        )

        try:
            resp = await llm.ainvoke([
                SystemMessage(content=REFLECTOR_SYSTEM),
                HumanMessage(content=eval_prompt),
            ])
            content = resp.content.strip()
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        content = part
                        break
            data = json.loads(content)
            decision = data.get("decision", "done")
            reflection_text = data.get("reflection", "")
        except Exception as e:
            logger.warning("Reflector 失败: %s，默认完成", e)
            decision = "done"
            reflection_text = "评估完成"

        if decision not in ("done", "continue", "retry"):
            decision = "done"

        # 更新计划状态
        updated_plan = list(plan)
        result: dict = {"reflection": reflection_text}

        if decision == "done":
            updated_plan = _mark_step(updated_plan, current_idx, "done")
            result["reflector_decision"] = "done"

        elif decision == "continue":
            updated_plan = _mark_step(updated_plan, current_idx, "done")
            next_idx = current_idx + 1
            if next_idx < total:
                updated_plan = _mark_step(updated_plan, next_idx, "running")
                # 向 messages 注入下一步骤指令（add_messages reducer 会追加）
                next_step = updated_plan[next_idx]
                step_msg = HumanMessage(
                    content=(
                        f"步骤 {current_idx + 1} 已完成。\n\n"
                        f"**[执行步骤 {next_idx + 1}/{total}]: {next_step['title']}**\n"
                        f"具体任务：{next_step['description']}\n"
                        "请完成此步骤。若需要新信息则使用工具；若已有足够上下文，直接给出结论。"
                    )
                )
                result["messages"] = [step_msg]
                result["current_step_index"] = next_idx
                result["step_iterations"] = 0
            else:
                result["reflector_decision"] = "done"
                result["current_step_index"] = next_idx
            result["reflector_decision"] = "continue" if next_idx < total else "done"

        elif decision == "retry":
            updated_plan = _mark_step(updated_plan, current_idx, "running")
            result["reflector_decision"] = "retry"
            result["step_iterations"] = step_iters + 1

        result["plan"] = updated_plan
        return result

    return reflector


def _mark_step(plan: list, idx: int, status: str) -> list:
    """返回将指定步骤状态设为 status 的新计划列表"""
    updated = list(plan)
    if 0 <= idx < len(updated):
        updated[idx] = {**updated[idx], "status": status}
    return updated


# ── 节点 5：保存回复 ──────────────────────────────────────────────────────────

async def _describe_images_for_storage(images: list[str], model: str) -> str:
    """
    用视觉 LLM 生成图片内容的简短描述，用于替代存储/记忆中的原始 base64 数据。
    同样使用 OpenAI SDK 直接调用，避免 LangChain 序列化问题。
    失败时静默降级，返回通用占位文本。
    """
    try:
        from openai import AsyncOpenAI

        vision_model = VISION_MODEL or model
        content: list = [
            {"type": "image_url", "image_url": {
                "url": img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
            }}
            for img in images
        ]
        content.append({"type": "text", "text": "请简短描述图片的主要内容，不超过50个字，直接描述，不要解释。"})

        client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
        resp = await client.chat.completions.create(
            model=vision_model,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip() or "图片内容"
    except Exception as exc:
        logger.warning("图片描述生成失败: %s", exc)
        return "图片内容"


def _strip_think_blocks(text: str) -> str:
    """移除 <think>...</think> 推理块（qwen3 等模型的思考内容不应存入上下文）"""
    import re
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _sanitize_for_db(text: str) -> str:
    """移除 PostgreSQL UTF-8 不支持的字符（null 字节等）。
    防止 fetch_webpage 抓到二进制内容后污染 full_response 导致存库失败。"""
    return text.replace('\x00', '').replace('\u0000', '')


async def save_response(state: GraphState) -> dict:
    """
    将本轮用户消息和 AI 最终回复追加到 ConversationStore 并持久化。
    同时保存工具调用事件到 tool_events 表供前端历史查看。
    """
    conv_id = state["conv_id"]
    client_id = state.get("client_id", "")
    user_msg = state["user_message"]
    images = state.get("images", [])
    full_response = _strip_think_blocks(state.get("full_response", ""))

    # 对话链路日志
    from logging_config import get_conv_logger
    clog = get_conv_logger(client_id, conv_id)
    route = state.get("route", "chat")
    plan = state.get("plan", [])
    tool_events_list = _extract_tool_events(state)
    tool_names_list = [ev["tool_name"] for ev in tool_events_list]
    clog.info(
        "对话完成 | route=%s | model=%s | plan_steps=%d | tools=%s | response_len=%d | images=%d | user_msg=%.60s",
        route,
        state.get("answer_model", state.get("model", "")),
        len(plan),
        tool_names_list,
        len(full_response),
        len(images),
        user_msg,
    )

    # 含图片时生成描述占位符，避免将原始 base64 存入 DB/记忆
    if images:
        model_for_desc = state.get("answer_model") or state["model"]
        img_desc = await _describe_images_for_storage(images, model_for_desc)
        placeholder = f"[用户上传了图片：图片内容大致为{img_desc}]"
        user_msg_to_save = f"{user_msg}\n{placeholder}" if user_msg.strip() else placeholder
    else:
        user_msg_to_save = user_msg

    await memory_store.add_message(conv_id, "user", _sanitize_for_db(user_msg_to_save))
    if full_response:
        tool_summary = _build_tool_summary(state)
        content_to_save = full_response
        if tool_summary:
            content_to_save = full_response + "\n\n" + tool_summary
        await memory_store.add_message(conv_id, "assistant", _sanitize_for_db(content_to_save))

    # 保存工具调用事件（供前端历史展示）
    if tool_events_list:
        from memory.tool_events import save_tool_event
        for ev in tool_events_list:
            await save_tool_event(conv_id, ev["tool_name"], ev["tool_input"])

    # 写回语义缓存
    # - chat/code：永不过期（ttl=None）
    # - search/search_code：带 TTL，到期后重新搜索（实时数据有时效性）
    # - 含图片不缓存（语义随图片内容变化）
    # - 含工具调用残留文本不缓存（模型未走 function calling 的脏数据）
    _TOOL_CALL_ARTIFACTS = ("[TOOL_CALL]", "minimax:tool_call", "<tool_call>", "[/TOOL_CALL]")
    _has_artifact = any(a in full_response for a in _TOOL_CALL_ARTIFACTS)
    if _has_artifact:
        import re as _re
        _cleaned = _re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", full_response, flags=_re.DOTALL)
        _cleaned = _re.sub(r"minimax:tool_call.*", "", _cleaned, flags=_re.DOTALL)
        _cleaned = _re.sub(r"<tool_call>.*?</tool_call>", "", _cleaned, flags=_re.DOTALL)
        _cleaned = _cleaned.strip()
        if _cleaned:
            logger.warning("ARTIFACT CLEAN | 工具调用残留文本已清理后写入DB | response='%.100s'", full_response)
            full_response = _cleaned
        else:
            logger.warning("ARTIFACT SKIP | 响应清理后为空，跳过DB写入 | response='%.100s'", full_response)
            full_response = ""

    _route = state.get("route", "chat")
    if full_response and not state.get("cache_hit") and not images and not _has_artifact:
        try:
            from cache.factory import get_cache
            cache     = get_cache()
            conv      = memory_store.get(conv_id)
            namespace = _derive_cache_namespace(conv, SEMANTIC_CACHE_NAMESPACE_MODE, client_id)
            ttl = (
                SEMANTIC_CACHE_SEARCH_TTL_HOURS * 3600
                if _route in ("search", "search_code")
                else None
            )
            await cache.store(user_msg, full_response, namespace, ttl_seconds=ttl)
        except Exception as exc:
            logger.warning("写入语义缓存失败（不影响主流程）: %s", exc)

    return {}


def _extract_tool_events(state: GraphState) -> list[dict]:
    """从 messages 中提取工具调用事件列表（用于持久化到 tool_events 表）"""
    messages = list(state.get("messages", []))
    events = []
    for m in messages:
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                if name:
                    events.append({"tool_name": name, "tool_input": args or {}})
    return events


def _build_tool_summary(state: GraphState) -> str:
    """从 messages 中提取工具调用摘要，用于上下文持久化"""
    messages = list(state.get("messages", []))
    summaries = []
    for m in messages:
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                summaries.append(f"- 调用工具: {name}({json.dumps(args, ensure_ascii=False)[:200]})")
        # ToolMessage
        if type(m).__name__ == "ToolMessage":
            content = str(m.content)[:300]
            summaries.append(f"  结果: {content}")

    if summaries:
        return "【工具调用记录】\n" + "\n".join(summaries[:20])  # 最多 20 条
    return ""


# ── 节点 6：压缩记忆 ──────────────────────────────────────────────────────────

async def compress_memory(state: GraphState) -> CompressNodeOutput:
    """
    按需触发对话压缩：
      - 对超过阈值的旧消息生成摘要
      - 同时将这批消息写入 Qdrant 长期记忆
    不影响流式输出（在 save_response 之后运行）。
    """
    conv_id = state["conv_id"]
    client_id = state.get("client_id", "")
    try:
        compressed = await maybe_compress(conv_id)
        if compressed:
            from logging_config import get_conv_logger
            get_conv_logger(client_id, conv_id).info("记忆压缩触发 conv=%s", conv_id)
    except Exception as exc:
        logger.error("压缩失败 conv=%s: %s", conv_id, exc)
        compressed = False
    return {"compressed": compressed}
