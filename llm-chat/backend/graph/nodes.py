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
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from config import LONGTERM_MEMORY_ENABLED, ROUTER_MODEL, ROUTE_MODEL_MAP, SEARCH_MODEL
from llm.chat import get_chat_llm
from graph.state import GraphState, PlanStep
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

    needs_tools = route in ("search", "search_code")
    tool_model = SEARCH_MODEL if needs_tools else answer_model

    from logging_config import get_conv_logger
    get_conv_logger(state.get("client_id", ""), state.get("conv_id", "")).info(
        "路由决策 | route=%s | tool_model=%s | answer_model=%s | user_msg=%.60s",
        route, tool_model, answer_model, user_msg,
    )

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
                    recent = [m.content for m in conv.messages if m.role == "user"][-2:]
                    if recent:
                        relevant = await rag_retriever.is_relevant_to_recent(user_msg, recent)
                    else:
                        relevant = True
                forget_mode = not relevant

        history_messages = build_messages(conv, long_term, forget_mode, tool_names)
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

PLANNER_SYSTEM = """你是一个任务规划专家。分析用户的请求，制定清晰的执行计划。

要求：
- 将任务分解为 2-5 个具体可执行的步骤
- 每步有明确的操作（搜索信息、获取数据、计算、分析、撰写等）
- 步骤间有逻辑顺序，后一步依赖前一步的结果
- 如果任务很简单只需一步操作，就只列 1 个步骤

输出格式（JSON）：
{"steps": [{"title": "简短标题（10字以内）", "description": "具体描述（说明要做什么、搜索什么）"}]}

只输出 JSON，不要任何解释。"""


def make_planner():
    """工厂函数：创建 planner 节点（任务规划）"""

    async def planner(state: GraphState) -> dict:
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
        model = state.get("tool_model") or state["model"]

        llm = get_chat_llm(model=model, temperature=0.1)
        response = await llm.ainvoke([
            SystemMessage(content=PLANNER_SYSTEM),
            HumanMessage(content=user_msg),
        ])

        plan_steps: list[PlanStep] = []
        try:
            content = response.content.strip()
            # 提取 JSON（去除 markdown code block）
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
            for i, s in enumerate(data.get("steps", [])):
                plan_steps.append(PlanStep(
                    id=str(i + 1),
                    title=s.get("title", f"步骤 {i + 1}"),
                    description=s.get("description", ""),
                    status="pending",
                    result="",
                ))
        except Exception as e:
            logger.warning("Planner JSON 解析失败: %s，使用单步兜底", e)
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
        llm = get_chat_llm(model=model, temperature=temperature)

        use_tools = tools and (not route or route in ("search", "search_code"))
        llm_with_tools = llm.bind_tools(tools) if use_tools else llm

        messages = list(state["messages"])

        # 若有执行计划且处于初始调用（第一步），注入步骤上下文
        plan = state.get("plan", [])
        current_idx = state.get("current_step_index", 0)
        step_iters = state.get("step_iterations", 0)

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
            if messages and hasattr(messages[-1], '__class__') and \
               messages[-1].__class__.__name__ == 'HumanMessage':
                messages[-1] = HumanMessage(content=str(messages[-1].content) + step_ctx)

        response = await llm_with_tools.ainvoke(messages)

        content = response.content if isinstance(response.content, str) else ""
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

        llm = get_chat_llm(model=model, temperature=temperature)

        messages = list(state["messages"])
        # 保留更多上下文（多步执行场景需要早期工具结果）
        messages = messages[-10:]
        response = await llm.ainvoke(messages)

        content = response.content if isinstance(response.content, str) else ""

        return {
            "messages": [response],
            "full_response": content,
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

    async def reflector(state: GraphState) -> dict:
        plan = state.get("plan", [])

        # 无计划时直接完成
        if not plan:
            return {"reflector_decision": "done", "reflection": "任务完成"}

        current_idx = state.get("current_step_index", 0)
        step_iters = state.get("step_iterations", 0)
        total = len(plan)
        full_response = state.get("full_response", "")

        # 安全边界：超出步骤范围或超过重试次数，强制完成
        if current_idx >= total or step_iters >= 2:
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
                        "请使用工具完成此步骤。"
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

async def save_response(state: GraphState) -> dict:
    """
    将本轮用户消息和 AI 最终回复追加到 ConversationStore 并持久化。
    同时保存工具调用事件到 tool_events 表供前端历史查看。
    """
    conv_id = state["conv_id"]
    client_id = state.get("client_id", "")
    user_msg = state["user_message"]
    full_response = state.get("full_response", "")

    # 对话链路日志
    from logging_config import get_conv_logger
    clog = get_conv_logger(client_id, conv_id)
    route = state.get("route", "chat")
    plan = state.get("plan", [])
    tool_events_list = _extract_tool_events(state)
    tool_names = [ev["tool_name"] for ev in tool_events_list]
    clog.info(
        "对话完成 | route=%s | model=%s | plan_steps=%d | tools=%s | response_len=%d | user_msg=%.60s",
        route,
        state.get("answer_model", state.get("model", "")),
        len(plan),
        tool_names,
        len(full_response),
        user_msg,
    )

    await memory_store.add_message(conv_id, "user", user_msg)
    if full_response:
        tool_summary = _build_tool_summary(state)
        content_to_save = full_response
        if tool_summary:
            content_to_save = full_response + "\n\n" + tool_summary
        await memory_store.add_message(conv_id, "assistant", content_to_save)

    # 保存工具调用事件（供前端历史展示）
    if tool_events_list:
        from memory.tool_events import save_tool_event
        for ev in tool_events_list:
            await save_tool_event(conv_id, ev["tool_name"], ev["tool_input"])

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

async def compress_memory(state: GraphState) -> dict:
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
