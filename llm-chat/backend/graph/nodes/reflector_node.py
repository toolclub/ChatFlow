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
from graph.nodes.base import BaseNode, track_usage
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

    @track_usage
    async def execute(self, state: GraphState) -> ReflectorNodeOutput:
        """
        核心反思逻辑：
        """
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
                    state.get("conv_id", ""), current_idx, step_iters,
                )
                updated_plan = self._mark_step(plan, current_idx, "running")
                return {
                    "reflector_decision": "retry",
                    "reflection":         "最后一步工具执行失败，尝试修复并重新执行",
                    "plan":               updated_plan,
                    "step_iterations":    step_iters + 1,
                }

            # 正常完成：标记最后一步完成
            updated_plan = self._mark_step(plan, current_idx, "done")
            return {
                "reflector_decision": "done",
                "reflection":         "最后一步执行完成",
                "plan":               updated_plan,
            }

        # ── 快速路径 4：非最后步骤 + 有响应 + 首次执行 → 直接推进 ─────────────────
        if not is_last and full_response and step_iters == 0:
            updated_plan = self._mark_step(plan, current_idx, "done")
            # 下一步自动设为 running
            updated_plan = self._mark_step(updated_plan, current_idx + 1, "running")
            # 注入下一步指令
            messages = self._build_next_step_messages(state, updated_plan, current_idx + 1)
            
            logger.info(
                "reflector continue (fast-path) | conv=%s | step=%d -> %d",
                state.get("conv_id", ""), current_idx, current_idx + 1,
            )
            return {
                "reflector_decision": "continue",
                "reflection":         f"步骤 {current_idx + 1} 完成，推进到步骤 {current_idx + 2}",
                "plan":               updated_plan,
                "current_step_index": current_idx + 1,
                "step_iterations":    0,
                "messages":           messages,
            }

        # ── 快速路径 5：无响应 + 可重试 → retry ──────────────────────────────
        if not full_response and step_iters < _MAX_STEP_ITERATIONS - 1:
            logger.info(
                "reflector retry (no response) | conv=%s | step=%d | iters=%d",
                state.get("conv_id", ""), current_idx, step_iters,
            )
            updated_plan = self._mark_step(plan, current_idx, "running")
            return {
                "reflector_decision": "retry",
                "reflection":         "未获得有效响应，尝试重新执行当前步骤",
                "plan":               updated_plan,
                "step_iterations":    step_iters + 1,
            }

        # ── LLM 评估路径 ──────────────────────────────────────────────────────
        # 触发场景：重试中（step_iters > 0）且获得了响应，需要判断是否改进到可接受
        logger.info(
            "reflector LLM 评估触发 | conv=%s | step=%d | iters=%d",
            state.get("conv_id", ""), current_idx, step_iters,
        )
        return await self._evaluate_with_llm(state, plan, current_idx, step_iters, full_response)

    async def _evaluate_with_llm(
        self, state: GraphState, plan: list[PlanStep], 
        idx: int, iters: int, response: str,
    ) -> ReflectorNodeOutput:
        """调用 LLM 评估当前步骤执行质量。"""
        model = state.get("tool_model") or state["model"]
        llm = get_chat_llm(model=model, temperature=0.0)

        step = plan[idx]
        user_msg = state.get("user_message", "")
        
        # 构建评估提示
        prompt = (
            f"用户原始需求：{user_msg}\n\n"
            f"当前正在执行第 {idx + 1} 步：{step['title']}\n"
            f"步骤描述：{step['description']}\n\n"
            f"模型给出的执行结果：\n---\n{response}\n---\n\n"
            "请评估该结果是否完成了本步骤的任务？\n"
            "若完成或基本完成（即使有小瑕疵但能继续），请输出：DONE | <简短理由>\n"
            "若完全没完成、逻辑严重错误或拒绝执行，请输出：RETRY | <具体改进建议>\n"
        )
        
        # 路由 model 也要披露思考过程
        messages = [
            {"role": "system", "content": _REFLECTOR_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        
        content = ""
        usage_data = {}
        try:
            # 评估过程也流式（披露反思逻辑）
            async for delta in llm.astream(messages, temperature=0.0):
                if isinstance(delta, dict) and "usage" in delta:
                    usage_data = delta["usage"]
                    continue
                if delta.startswith(BaseNode._THINK_PREFIX):
                    thinking_text = delta[len(BaseNode._THINK_PREFIX):]
                    await self.emit_thinking("reflector", "reasoning", thinking_text, idx)
                else:
                    content += delta
            
            raw = content.strip().upper()
        except Exception as exc:
            logger.warning("reflector LLM 评估异常: %s，降级到 done", exc)
            raw = "DONE"

        if "RETRY" in raw and iters < _MAX_STEP_ITERATIONS - 1:
            decision = "retry"
            reflection = raw.split("|")[-1].strip() if "|" in raw else "建议重试以改进质量"
            updated_plan = self._mark_step(plan, idx, "running")
            return {
                "reflector_decision": "retry",
                "reflection":         reflection,
                "plan":               updated_plan,
                "step_iterations":    iters + 1,
                "usage":              usage_data,
            }
        else:
            decision = "done"
            reflection = raw.split("|")[-1].strip() if "|" in raw else "步骤执行通过"
            updated_plan = self._mark_step(plan, idx, "done")
            
            if idx < len(plan) - 1:
                # 还有下一步，推进
                updated_plan = self._mark_step(updated_plan, idx + 1, "running")
                messages = self._build_next_step_messages(state, updated_plan, idx + 1)
                return {
                    "reflector_decision": "continue",
                    "reflection":         reflection,
                    "plan":               updated_plan,
                    "current_step_index": idx + 1,
                    "step_iterations":    0,
                    "messages":           messages,
                    "usage":              usage_data,
                }
            else:
                # 全部完成
                return {
                    "reflector_decision": "done",
                    "reflection":         reflection,
                    "plan":               updated_plan,
                    "usage":              usage_data,
                }

    def _build_next_step_messages(self, state: GraphState, plan: list[PlanStep], next_idx: int) -> list:
        """
        构建进入下一步时所需的聚焦消息指令。
        
        策略：在 messages 中追加一条 HumanMessage，明确告知"上一步已完成，现在开始下一步"。
        """
        from langchain_core.messages import HumanMessage
        
        prev_step = plan[next_idx - 1]
        next_step = plan[next_idx]
        
        # 提取上一步的执行结果摘要（避免 full_response 过长淹没指令）
        res = state.get("full_response", "")
        res_summary = (res[:_STEP_RESULT_SUMMARY_LEN] + "...") if len(res) > _STEP_RESULT_SUMMARY_LEN else res
        
        instruction = (
            f"✅ **[步骤 {next_idx} 已完成]**: {prev_step['title']}\n"
            f"执行结果摘要：\n{res_summary}\n\n"
            f"---\n\n"
            f"🚀 **[现在开始执行步骤 {next_idx + 1}/{len(plan)}]**: {next_step['title']}\n"
            f"具体任务：{next_step['description']}\n"
            f"请开始执行。"
        )
        
        return [HumanMessage(content=instruction)]

    async def _last_tool_failed(self, state: GraphState) -> bool:
        """Use persisted execution states, including timeouts and parallel failures."""
        return await self._latest_tool_batch_failed(state)

    def _build_fallback_response(self, state: GraphState) -> str:
        """
        从工具结果中提取信息，为没有 content 的回复构建兜底文本。
        """
        messages = list(state.get("messages") or [])
        parts = []
        for m in messages[-5:]:  # 只看最近几条
            if type(m).__name__ == "ToolMessage":
                content = str(m.content)
                # 提取前 300 字作为摘要
                summary = content[:300] + ("..." if len(content) > 300 else "")
                parts.append(f"[工具执行结果]: {summary}")
        
        if not parts:
            return ""
            
        return "模型已执行以下工具操作：\n\n" + "\n\n".join(parts)
