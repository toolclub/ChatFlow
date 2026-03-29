"""
LangGraph 条件边：决定节点之后的路由
"""
from graph.state import GraphState


def should_continue(state: GraphState) -> str:
    """
    call_model 节点后的路由：
      - 有工具调用 → "tools"（执行工具）
      - 无工具调用且有计划 → "reflector"（评估步骤完成情况）
      - 无工具调用且无计划 → "save_response"（直接保存）
    """
    messages = state.get("messages", [])
    if not messages:
        return "reflector" if state.get("plan") else "save_response"

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"

    # 有计划时先反思，再决定是否继续下一步
    if state.get("plan"):
        return "reflector"

    return "save_response"


def should_continue_after_tool(state: GraphState) -> str:
    """
    call_model_after_tool 节点后的路由：
      - 有工具调用 → "tools"（继续执行更多工具）
      - 无工具调用且有计划 → "reflector"
      - 无工具调用且无计划 → "save_response"
    """
    messages = state.get("messages", [])
    if not messages:
        return "reflector" if state.get("plan") else "save_response"

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"

    if state.get("plan"):
        return "reflector"

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
