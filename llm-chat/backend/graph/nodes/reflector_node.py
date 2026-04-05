"""
ReflectorNode：任务反思评估节点

职责：
  - 评估当前步骤的执行结果
  - 决策：done（完成）/ continue（继续下一步）/ retry（重试）
  - 更新计划步骤状态
  - continue 时向 messages 注入下一步骤指令（add_messages reducer 会追加）

快速路径：
  - 无计划 → 直接 done
  - 超出边界 or 超过重试次数 → 强制 done
  - 最后一步且有响应 → 直接 done
  - 非最后步骤 + 有工具结果 + 首次执行 → 直接 continue（不调 LLM）

最后一条快速路径是关键优化：call_model_after_tool 的输出常包含"下一步行动"
等措辞，若让 LLM 评估会误认为后续步骤已处理，导致提前 done。
"""
import json
import logging

from langchain_core.messages import HumanMessage

from graph.event_types import ReflectorNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState, PlanStep
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.nodes.reflector")

_REFLECTOR_SYSTEM = """你是一个任务完成情况评估专家。

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

# 每步最多重试次数
_MAX_STEP_ITERATIONS = 3


class ReflectorNode(BaseNode):
    """任务反思评估节点：评估步骤执行结果并决定下一步路由。"""

    @property
    def name(self) -> str:
        return "reflector"

    async def execute(self, state: GraphState) -> ReflectorNodeOutput:
        """
        反思评估逻辑：
          1. 无计划 → 直接 done
          2. 边界检查 → 强制 done
          3. 快速路径检查 → 跳过 LLM 直接决策
          4. 调用 LLM 评估
          5. 更新计划状态并注入步骤指令
        """
        plan = state.get("plan", [])

        # 无计划时直接完成
        if not plan:
            return {"reflector_decision": "done", "reflection": "任务完成"}

        current_idx   = state.get("current_step_index", 0)
        step_iters    = state.get("step_iterations", 0)
        total         = len(plan)
        full_response = state.get("full_response", "")

        # ── 安全边界：超出范围或超过重试次数，强制完成 ──────────────────────
        if current_idx >= total or step_iters >= _MAX_STEP_ITERATIONS:
            updated_plan = self._mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection":         "步骤执行完成（达到边界条件）",
                "plan":               updated_plan,
            }

        is_last = current_idx >= total - 1

        # ── 快速路径 1：最后一步且有响应 → done ────────────────────────────
        if is_last and full_response:
            updated_plan = self._mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection":         "最后步骤执行完成",
                "plan":               updated_plan,
            }

        # ── 快速路径 2：非最后步骤 + 有工具结果 + 首次执行 → continue ──────
        # 不调 LLM，直接 continue（防止 call_model_after_tool 输出误导评估）
        messages = list(state.get("messages", []))
        recent   = messages[-5:] if len(messages) > 5 else messages

        if not is_last and step_iters == 0:
            has_tool_result = any(
                type(m).__name__ == "ToolMessage"
                for m in recent
            )
            if has_tool_result:
                return self._make_continue_result(plan, current_idx, total)

        # ── 调用 LLM 评估 ────────────────────────────────────────────────────
        model = state.get("answer_model") or state.get("model", "")
        llm   = get_chat_llm(model=model, temperature=0.1)

        recent_text = "\n".join([
            f"[{type(m).__name__}]: {str(m.content)[:600]}"
            for m in recent
        ])
        current_step = plan[current_idx]
        eval_prompt  = (
            f"执行计划共 {total} 步，当前步骤 {current_idx + 1}：{current_step['title']}\n"
            f"步骤描述：{current_step['description']}\n\n"
            f"最近执行记录：\n{recent_text}\n\n"
            f"是否还有后续步骤：{'是' if not is_last else '否（这是最后一步）'}"
        )

        messages_for_llm = [
            {"role": "system", "content": _REFLECTOR_SYSTEM},
            {"role": "user",   "content": eval_prompt},
        ]

        try:
            completion = await llm.ainvoke(messages_for_llm)
            raw = (completion.choices[0].message.content or "").strip()

            # 去除 markdown code block
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        raw = part
                        break

            data              = json.loads(raw)
            decision          = data.get("decision", "done")
            reflection_text   = data.get("reflection", "")
        except Exception as e:
            logger.warning("Reflector LLM 失败: %s，默认完成", e)
            decision        = "done"
            reflection_text = "评估完成"

        # 非法决策值回退到 done
        if decision not in ("done", "continue", "retry"):
            decision = "done"

        return self._build_result(plan, current_idx, total, decision, reflection_text, step_iters)

    def _make_continue_result(
        self,
        plan: list[PlanStep],
        current_idx: int,
        total: int,
    ) -> ReflectorNodeOutput:
        """快速 continue：当前步骤完成，直接推进到下一步，不调 LLM。"""
        updated_plan = self._mark_step(plan, current_idx, "done")
        next_idx     = current_idx + 1
        updated_plan = self._mark_step(updated_plan, next_idx, "running")
        next_step    = updated_plan[next_idx]

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
            "reflection":         f"步骤 {current_idx + 1} 工具调用完成，继续执行步骤 {next_idx + 1}",
            "plan":               updated_plan,
            "messages":           [step_msg],
            "current_step_index": next_idx,
            "step_iterations":    0,
        }

    def _build_result(
        self,
        plan: list[PlanStep],
        current_idx: int,
        total: int,
        decision: str,
        reflection_text: str,
        step_iters: int,
    ) -> ReflectorNodeOutput:
        """根据 LLM 决策构建完整的节点返回值。"""
        updated_plan = list(plan)
        result: dict = {"reflection": reflection_text}

        if decision == "done":
            updated_plan         = self._mark_step(updated_plan, current_idx, "done")
            result["reflector_decision"] = "done"

        elif decision == "continue":
            updated_plan = self._mark_step(updated_plan, current_idx, "done")
            next_idx     = current_idx + 1
            if next_idx < total:
                updated_plan = self._mark_step(updated_plan, next_idx, "running")
                next_step    = updated_plan[next_idx]
                step_msg     = HumanMessage(
                    content=(
                        f"步骤 {current_idx + 1} 已完成。\n\n"
                        f"**[执行步骤 {next_idx + 1}/{total}]: {next_step['title']}**\n"
                        f"具体任务：{next_step['description']}\n"
                        "请完成此步骤。若需要新信息则使用工具；若已有足够上下文，直接给出结论。"
                    )
                )
                result["messages"]           = [step_msg]
                result["current_step_index"] = next_idx
                result["step_iterations"]    = 0
            else:
                result["current_step_index"] = next_idx
            result["reflector_decision"] = "continue" if next_idx < total else "done"

        elif decision == "retry":
            updated_plan                 = self._mark_step(updated_plan, current_idx, "running")
            result["reflector_decision"] = "retry"
            result["step_iterations"]    = step_iters + 1

        result["plan"] = updated_plan
        return result
