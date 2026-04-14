"""
ReflectorNode：任务反思评估节点

职责：
  - 评估当前步骤的执行结果
  - 决策：done（完成）/ continue（继续下一步）/ retry（重试）
  - 更新计划步骤状态，累积 step_results
  - continue 时向 messages 注入下一步骤指令（含前序步骤摘要）

快速路径（绝大多数情况无需 LLM 调用）：
  1. 无计划 → 直接 done
  2. 超出边界 / 超过重试次数 → 强制 done
  3. 最后一步 + 有响应 → done（无需评估）
  4. 非最后步骤 + 有响应 + 首次执行 → continue（最常见情形，直接推进）
  5. 任意步骤 + 无响应 + 可重试 → retry（无需 LLM 判断）

LLM 评估仅在以下边缘场景触发：
  - 重试中（step_iters > 0）且有响应，需要判断重试结果是否足够好
  - 其他无法快速判断的边缘情形
"""
import json
import logging

from langchain_core.messages import HumanMessage

from graph.event_types import ReflectorNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState, PlanStep
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.nodes.reflector")

from prompts import load_prompt

_REFLECTOR_SYSTEM = load_prompt("nodes/reflector")

# 每步最多重试次数（超出后强制 done）
_MAX_STEP_ITERATIONS = 3

# 注入步骤指令时，前序步骤结果的摘要截断长度
_STEP_RESULT_SUMMARY_LEN = 2000


class ReflectorNode(BaseNode):
    """
    任务反思评估节点。

    通过分层快速路径大幅减少 LLM 调用：
    - 绝大多数步骤（有响应 + 首次执行）直接 continue/done，无 LLM 开销
    - 只有重试中的边缘场景才真正调用 LLM 进行质量评估
    """

    @property
    def name(self) -> str:
        return "reflector"

    async def execute(self, state: GraphState) -> ReflectorNodeOutput:
        plan = state.get("plan", [])

        # ── 快速路径 1：无计划 → 直接完成 ──────────────────────────────────────
        if not plan:
            return {"reflector_decision": "done", "reflection": "任务完成"}

        current_idx   = state.get("current_step_index", 0)
        step_iters    = state.get("step_iterations", 0)
        total         = len(plan)
        full_response = state.get("full_response", "")

        # ── 兜底：full_response 为空但有工具执行结果时，从 ToolMessage 中构建摘要 ──
        # 典型场景：工具调用上限截断 / 模型只输出 tool_calls 没有 content
        # 不构建的话 save_response 会存空 content → 刷新后消息丢失
        _fallback_applied = False
        if not full_response:
            full_response = self._build_fallback_response(state)
            if full_response:
                _fallback_applied = True
                logger.info(
                    "reflector 兜底：从工具结果构建 full_response | conv=%s | len=%d",
                    state.get("conv_id", ""), len(full_response),
                )

        # ── 快速路径 2：边界保护 → 强制完成 ─────────────────────────────────────
        if current_idx >= total or step_iters >= _MAX_STEP_ITERATIONS:
            updated_plan = self._mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection":         "步骤执行完成（达到边界条件）",
                "plan":               updated_plan,
            }

        is_last = current_idx >= total - 1

        # ── 快速路径 3：最后一步 + 有响应 ──────────────────────────────────────────
        # 检查最近工具执行是否失败：失败时 retry 而非 done，让模型有机会修复
        if is_last and full_response:
            if step_iters < _MAX_STEP_ITERATIONS - 1 and await self._last_tool_failed(state):
                logger.info(
                    "reflector retry (last-step tool failed) | conv=%s | step=%d | iters=%d",
                    state.get("conv_id", ""), current_idx + 1, step_iters,
                )
                updated_plan = self._mark_step(plan, current_idx, "running")
                return {
                    "reflector_decision": "retry",
                    "reflection":         "最后一步工具执行失败，重试以修复",
                    "plan":               updated_plan,
                    "step_iterations":    step_iters + 1,
                    "forget_mode":        False,
                }
            return await self._make_done_result(state, plan, current_idx, full_response)

        # ── 快速路径 4：非最后步骤 + 有响应 + 首次执行 → continue（最常见路径）──
        # 这覆盖了绝大多数正常多步执行场景，彻底消除 reflector LLM 调用
        if not is_last and full_response and step_iters == 0:
            return await self._make_continue_result(state, plan, current_idx, total, full_response)

        # ── 快速路径 5：无响应 + 可重试 → retry ──────────────────────────────────
        if not full_response and step_iters < _MAX_STEP_ITERATIONS - 1:
            logger.info(
                "reflector fast-retry | conv=%s | step=%d | iters=%d | 无响应重试",
                state.get("conv_id", ""), current_idx + 1, step_iters,
            )
            updated_plan = self._mark_step(plan, current_idx, "running")
            return {
                "reflector_decision": "retry",
                "reflection":         "步骤未产生响应，自动重试",
                "plan":               updated_plan,
                "step_iterations":    step_iters + 1,
                "forget_mode":        False,
            }

        # ── LLM 评估：仅用于重试中有响应的边缘场景 ───────────────────────────────
        return await self._llm_evaluate(state, plan, current_idx, total, step_iters, full_response, is_last)

    # ══════════════════════════════════════════════════════════════════════════
    # 结果构建
    # ══════════════════════════════════════════════════════════════════════════

    async def _make_done_result(
        self,
        state: GraphState,
        plan: list[PlanStep],
        current_idx: int,
        full_response: str,
    ) -> ReflectorNodeOutput:
        """标记当前步骤完成，累积 step_results，返回 done。"""
        updated_plan = self._mark_step(plan, current_idx, "done")
        updated_plan[current_idx] = {**updated_plan[current_idx], "result": full_response[:3000]}

        step_results = self._accumulate_step_results(state, full_response)
        await self._persist_step(state, current_idx, full_response, current_idx + 1)

        logger.info(
            "reflector done (fast) | conv=%s | step=%d/%d",
            state.get("conv_id", ""), current_idx + 1, len(plan),
        )
        result: ReflectorNodeOutput = {
            "reflector_decision": "done",
            "reflection":         "步骤执行完成",
            "plan":               updated_plan,
            "step_results":       step_results,
        }
        # 兜底 response 写回 state，确保 save_response 不存空内容
        if full_response and not state.get("full_response"):
            result["full_response"] = full_response
        return result

    async def _make_continue_result(
        self,
        state: GraphState,
        plan: list[PlanStep],
        current_idx: int,
        total: int,
        full_response: str,
    ) -> ReflectorNodeOutput:
        """标记当前步骤完成，构建下一步指令（含前序步骤摘要），返回 continue。"""
        updated_plan = self._mark_step(plan, current_idx, "done")
        updated_plan[current_idx] = {**updated_plan[current_idx], "result": full_response[:3000]}

        next_idx     = current_idx + 1
        updated_plan = self._mark_step(updated_plan, next_idx, "running")

        step_results = self._accumulate_step_results(state, full_response)
        await self._persist_step(state, current_idx, full_response, next_idx)

        step_msg = self._build_step_message(updated_plan, current_idx, next_idx, total, step_results)

        logger.info(
            "reflector continue (fast) | conv=%s | step=%d→%d/%d",
            state.get("conv_id", ""), current_idx + 1, next_idx + 1, total,
        )
        result: ReflectorNodeOutput = {
            "reflector_decision": "continue",
            "reflection":         f"步骤 {current_idx + 1} 完成，继续步骤 {next_idx + 1}",
            "plan":               updated_plan,
            "messages":           [step_msg],
            "current_step_index": next_idx,
            "step_iterations":    0,
            "step_results":       step_results,
        }
        if full_response and not state.get("full_response"):
            result["full_response"] = full_response
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # LLM 评估（仅边缘场景）
    # ══════════════════════════════════════════════════════════════════════════

    async def _llm_evaluate(
        self,
        state: GraphState,
        plan: list[PlanStep],
        current_idx: int,
        total: int,
        step_iters: int,
        full_response: str,
        is_last: bool,
    ) -> ReflectorNodeOutput:
        """LLM 评估：仅在重试中有响应时调用，判断质量是否足够好。"""
        model = state.get("answer_model") or state.get("model", "")
        llm   = get_chat_llm(model=model, temperature=0.1)

        messages      = list(state.get("messages", []))
        recent        = messages[-5:] if len(messages) > 5 else messages
        recent_text   = "\n".join(
            f"[{type(m).__name__}]: {str(m.content)[:500]}" for m in recent
        )
        current_step  = plan[current_idx]
        eval_prompt   = (
            f"执行计划共 {total} 步，当前步骤 {current_idx + 1}：{current_step['title']}\n"
            f"步骤描述：{current_step['description']}\n\n"
            f"最近执行记录：\n{recent_text}\n\n"
            f"是否还有后续步骤：{'是' if not is_last else '否（这是最后一步）'}\n"
            f"已重试 {step_iters} 次。"
        )

        messages_for_llm = [
            {"role": "system", "content": _REFLECTOR_SYSTEM},
            {"role": "user",   "content": eval_prompt},
        ]

        from logging_config import log_prompt
        log_prompt(state.get("conv_id", ""), "reflector", model, messages_for_llm)

        decision        = "done"
        reflection_text = "评估完成"
        try:
            # 流式调用：逐 token 推送 thinking 事件给前端，避免长时间静默
            from langchain_core.callbacks.manager import adispatch_custom_event

            _THINK_PREFIX = "\x00THINK\x00"
            content_parts: list[str] = []
            async for delta in llm.astream(messages_for_llm, temperature=0.1):
                if delta.startswith(_THINK_PREFIX):
                    thinking_text = delta[len(_THINK_PREFIX):]
                    await adispatch_custom_event(
                        "llm_thinking", {"content": thinking_text, "node": "reflector"},
                    )
                else:
                    content_parts.append(delta)
                    await adispatch_custom_event(
                        "llm_thinking", {"content": delta, "node": "reflector"},
                    )
            raw = "".join(content_parts).strip()
            if not raw:
                raise ValueError("LLM 返回空内容")
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        raw = part
                        break
            data            = json.loads(raw)
            decision        = data.get("decision", "done")
            reflection_text = data.get("reflection", "")
            if decision not in ("done", "continue", "retry"):
                decision = "done"
        except Exception as exc:
            logger.warning("Reflector LLM 失败: %s，默认完成", exc)

        logger.info(
            "reflector LLM decision | conv=%s | step=%d/%d | decision=%s | iters=%d",
            state.get("conv_id", ""), current_idx + 1, total, decision, step_iters,
        )

        if decision == "continue" and not is_last:
            return await self._make_continue_result(state, plan, current_idx, total, full_response)
        elif decision == "retry" and step_iters < _MAX_STEP_ITERATIONS - 1:
            updated_plan = self._mark_step(plan, current_idx, "running")
            return {
                "reflector_decision": "retry",
                "reflection":         reflection_text,
                "plan":               updated_plan,
                "step_iterations":    step_iters + 1,
            }
        else:
            # done 或 continue 溢出边界 → 视为完成
            return self._make_done_result(state, plan, current_idx, full_response or "(步骤结果为空)")

    # ══════════════════════════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _build_fallback_response(state: GraphState) -> str:
        """
        从 messages 中的 ToolMessage 构建兜底响应摘要。

        当模型只产出了 tool_calls 没有 content 时（如工具上限截断），
        收集最近的工具执行结果作为 full_response，确保 save_response 不存空内容。
        """
        messages = state.get("messages", [])
        parts: list[str] = []

        # 从最后一条 HumanMessage 开始，收集本步骤的所有工具调用和结果
        for m in reversed(messages):
            msg_type = type(m).__name__
            if msg_type == "HumanMessage":
                break
            if msg_type == "AIMessage" and hasattr(m, "content") and m.content:
                parts.append(str(m.content)[:1000])
            elif msg_type == "ToolMessage" and hasattr(m, "content") and m.content:
                parts.append(str(m.content)[:500])

        if not parts:
            return ""
        parts.reverse()
        return "\n\n".join(parts)

    @staticmethod
    async def _last_tool_failed(state: GraphState) -> bool:
        """从 tool_executions DB 查询最近一次工具调用是否失败。"""
        conv_id = state.get("conv_id", "")
        if not conv_id:
            return False
        try:
            from db.tool_store import get_tool_executions_for_conv
            tool_execs = await get_tool_executions_for_conv(conv_id)
            if tool_execs:
                return tool_execs[-1].get("status") == "error"
        except Exception:
            pass
        return False

    @staticmethod
    def _accumulate_step_results(state: GraphState, full_response: str) -> list[str]:
        """将当前步骤结果追加到 step_results 列表（截断防止无限增长）。"""
        existing = list(state.get("step_results") or [])
        if full_response:
            existing.append(full_response[:3000])
        return existing

    @staticmethod
    async def _persist_step(
        state: GraphState,
        step_idx: int,
        full_response: str,
        next_step: int,
    ) -> None:
        """持久化步骤结果到 DB（失败仅记录日志，不影响主流程）。"""
        plan_id = state.get("plan_id", "")
        if not plan_id or not full_response:
            return
        try:
            from db.plan_store import save_step_result
            await save_step_result(plan_id, step_idx, full_response, next_step)
        except Exception as exc:
            logger.warning("DB 步骤结果写入失败（不影响主流程）: %s", exc)

    @staticmethod
    def _build_step_message(
        plan: list[PlanStep],
        done_idx: int,
        next_idx: int,
        total: int,
        step_results: list[str],
    ) -> HumanMessage:
        """
        构建步骤过渡指令消息。

        包含：
        - 已完成步骤的结果摘要（让模型知道之前做了什么）
        - 下一步骤的标题和描述
        - 是否为末步的行动指引
        """
        next_step    = plan[next_idx]
        is_next_last = next_idx >= total - 1

        # 前序步骤结果摘要（最多展示最近 3 步的摘要）
        summary_parts: list[str] = []
        for i, res in enumerate(step_results[-3:], start=max(0, done_idx - 2)):
            if res:
                short = res[:_STEP_RESULT_SUMMARY_LEN]
                if len(res) > _STEP_RESULT_SUMMARY_LEN:
                    short += "..."
                summary_parts.append(f"步骤 {i + 1} 结果摘要：{short}")

        summary_block = (
            "\n\n**已完成步骤摘要**\n" + "\n".join(summary_parts)
            if summary_parts else ""
        )

        if is_next_last:
            action_hint = (
                "请基于以上所有步骤的执行结果，生成完整的最终回复。"
                "直接输出最终内容，不要再调用工具。"
            )
        else:
            action_hint = (
                "请完成此步骤的任务：若需要新信息则使用工具；"
                "否则直接给出本步骤的摘要结论（不要提前生成最终回复）。"
            )

        content = (
            f"步骤 {done_idx + 1} 已完成。{summary_block}\n\n"
            f"**[执行步骤 {next_idx + 1}/{total}]: {next_step['title']}**\n"
            f"具体任务：{next_step['description']}\n\n"
            f"{action_hint}"
        )

        return HumanMessage(content=content)
