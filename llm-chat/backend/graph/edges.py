"""
LangGraph 条件边：决定节点之后的路由
"""
from graph.state import GraphState


def cache_routing(state: GraphState) -> str:
    """
    semantic_cache_check 节点后的路由：
      - cache_hit=True  → "save_response"（跳过 LLM 全流程，直接保存）
      - cache_hit=False → "after_cache"（继续正常流程，由 agent.py 映射到实际节点）
    """
    if state.get("cache_hit"):
        return "save_response"
    return "after_cache"


def should_continue(state: GraphState) -> str:
    """
    call_model 节点后的路由：
      - 有工具调用 → "tools"（执行工具）
      - 无工具调用且有计划 → "reflector"（评估步骤完成情况）
      - 无工具调用且无计划 → "save_response"（直接保存）
    """
    import logging
    _log = logging.getLogger("graph.edges")

    messages = state.get("messages", [])
    conv_id = state.get("conv_id", "")

    if not messages:
        decision = "reflector" if state.get("plan") else "save_response"
        _log.info("should_continue | conv=%s | no messages → %s", conv_id, decision)
        return decision

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        names = [tc.get("name","") if isinstance(tc,dict) else getattr(tc,"name","") for tc in last.tool_calls]
        _log.info("should_continue | conv=%s | tool_calls=%s → tools", conv_id, names)
        return "tools"

    if state.get("plan"):
        _log.info("should_continue | conv=%s | no tool_calls, has plan → reflector", conv_id)
        return "reflector"

    _log.info("should_continue | conv=%s | no tool_calls, no plan → save_response", conv_id)
    return "save_response"


_MAX_TOOL_CALLS_PER_STEP = 50  # 计划模式下每步最多调用工具次数（复杂代码任务可能需要多轮写文件+执行+调试）


def should_continue_after_tool(state: GraphState) -> str:
    """
    call_model_after_tool 节点后的路由：
      - 有工具调用 → "tools"（继续执行更多工具）
      - 无工具调用且有计划 → "reflector"
      - 无工具调用且无计划 → "save_response"

    计划模式下限制每步最多 _MAX_TOOL_CALLS_PER_STEP 次工具调用，
    防止模型在单步内无限循环搜索，导致上下文爆炸和步骤边界丢失。
    """
    import logging
    _log = logging.getLogger("graph.edges")

    plan = state.get("plan", [])
    messages = state.get("messages", [])
    conv_id = state.get("conv_id", "")

    if not messages:
        decision = "reflector" if plan else "save_response"
        _log.info("should_continue_after_tool | conv=%s | no messages → %s", conv_id, decision)
        return decision

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        names = [tc.get("name","") if isinstance(tc,dict) else getattr(tc,"name","") for tc in last.tool_calls]

        # 计划模式：统计当前步骤内已使用的工具次数
        # "步骤边界" = 上一条 HumanMessage（来自 reflector 的步骤指令或原始用户消息）
        if plan:
            tool_count = 0
            for m in reversed(messages[:-1]):  # 跳过当前 AIMessage（含新 tool_calls）
                msg_type = type(m).__name__
                if msg_type == "HumanMessage":
                    break
                if msg_type == "ToolMessage":
                    tool_count += 1
            if tool_count >= _MAX_TOOL_CALLS_PER_STEP:
                _log.warning(
                    "should_continue_after_tool | conv=%s | 步骤工具调用已达上限(%d次)，忽略新 tool_calls → reflector",
                    conv_id, tool_count,
                )
                return "reflector"

        _log.info("should_continue_after_tool | conv=%s | tool_calls=%s → tools", conv_id, names)
        return "tools"

    if plan:
        _log.info("should_continue_after_tool | conv=%s | no tool_calls, has plan → reflector", conv_id)
        return "reflector"

    _log.info("should_continue_after_tool | conv=%s | no tool_calls, no plan → save_response", conv_id)
    return "save_response"


def reflector_routing(state: GraphState) -> str:
    """
    reflector 节点后的路由：
      - "continue" 或 "retry" → "call_model"（继续执行下一步或重试）
      - 其他（"done"）→ "save_response"（完成，保存结果）
    """
    decision = state.get("reflector_decision", "done")
    if decision in ("continue", "retry"):
        return "call_model"
    return "save_response"
