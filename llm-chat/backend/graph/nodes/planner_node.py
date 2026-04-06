"""
PlannerNode：任务规划节点

职责：
  - 分析用户请求，生成结构化执行计划（步骤列表）
  - 仅对 search / search_code 路由（或无路由模式）触发规划
  - 读取 state["vision_description"]（由 VisionNode 预生成）作为图片内容输入，
    避免重复调用视觉模型
  - 三层兜底：重试拿非空内容 → 解析 JSON → 单步兜底

硬性限制：计划最多 3 步，防止步骤过多触发 nginx 超时。
"""
import json
import logging
import re
from datetime import date

from config import SEARCH_MODEL, VISION_MODEL
from graph.event_types import PlannerNodeOutput
from graph.nodes.base import BaseNode
from graph.state import GraphState, PlanStep
from llm.chat import get_chat_llm

logger = logging.getLogger("graph.nodes.planner")

def _fix_json_inner_quotes(text: str) -> str:
    """
    修复 JSON 字符串值内的裸 ASCII 双引号。

    模型有时在字段值中用双引号包裹词语（如 "百业"），
    导致 json.loads 报 'Expecting comma' 错误。

    策略：状态机逐字符扫描，进入字符串后若遇到双引号，
    检查其后紧跟的字符——若不是 JSON 结构字符（, } ] :），
    则认为是内部装饰引号，替换为单引号。
    """
    result: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == '\\' and in_string:
            # 转义序列，原样保留
            result.append(c)
            i += 1
            if i < len(text):
                result.append(text[i])
            i += 1
            continue
        if c == '"':
            if not in_string:
                in_string = True
                result.append(c)
            else:
                # 判断是字符串结束引号，还是值内部的裸双引号
                # 字符串结束后，下一个非空字符必然是 , } ] :（JSON 结构符）
                j = i + 1
                while j < len(text) and text[j] in (' ', '\t', '\n', '\r'):
                    j += 1
                next_ch = text[j] if j < len(text) else ''
                if next_ch in (',', '}', ']', ':'):
                    # 合法的字符串结束引号
                    in_string = False
                    result.append(c)
                else:
                    # 内部裸引号，替换为单引号
                    result.append("'")
        else:
            result.append(c)
        i += 1
    return ''.join(result)


# ── 今日日期（构建时固定，进程生命周期内不变） ─────────────────────────────────
_TODAY = date.today().strftime("%Y年%m月%d日")

_PLANNER_SYSTEM = f"""你是一个任务规划专家。分析用户的请求，制定清晰的执行计划。
当前日期：{_TODAY}。搜索时直接用核心关键词，不要手动添加年份。
要求：
- 将任务分解为若干个具体可执行的步骤
- 每步有明确的操作（搜索信息、获取数据、计算、分析、撰写等）
- 步骤间有逻辑顺序，后一步依赖前一步的结果
- 如果任务很简单只需一步操作，就只列 1 个步骤
- 【最后一步原则】如果用户最终需要获得某个产品（网页、完整代码、分析报告等），
  最后一步必须是"直接输出该产品的完整内容（代码/HTML/文档）"；
  不能用"规划结构"、"设计架构"、"制定方案"等描述性步骤代替；
  前面的步骤专门负责收集或分析所需信息，最后步才是生成

输出格式（JSON）：
{{"steps": [{{"title": "简短标题（10字以内）", "description": "具体描述（说明要做什么、搜索什么）"}}]}}

只输出 JSON，不要任何解释。"""

# 每步最多计划步数（stream.py recursion_limit=120，每步约4节点，10步≈45节点，远低于上限）
_MAX_PLAN_STEPS = 10


class PlannerNode(BaseNode):
    """任务规划节点：将搜索类请求分解为步骤化执行计划。"""

    @property
    def name(self) -> str:
        return "planner"

    async def execute(self, state: GraphState) -> PlannerNodeOutput:
        """
        规划执行步骤。

        chat / code 路由无需规划，直接返回空计划。
        search / search_code 路由调用 LLM 生成步骤。
        """
        route = state.get("route", "")

        # 非搜索类路由跳过规划
        needs_planning = not route or route in ("search", "search_code")
        if not needs_planning:
            return {
                "plan":               [],
                "current_step_index": 0,
                "step_iterations":    0,
            }

        user_msg    = state["user_message"]
        images      = state.get("images", [])
        vision_desc = state.get("vision_description", "")

        # ── 规划模型选择 ────────────────────────────────────────────────────
        # 若路由分配的模型是视觉模型（仅在 VISION_BASE_URL 上存在），
        # 规划模型必须切换到主接口上的模型，否则无法访问。
        model = state.get("tool_model") or state["model"]
        if model == (VISION_MODEL or "") or not model:
            model = SEARCH_MODEL or state["model"]

        # ── 网页澄清预检（search_code 路由兼容方案） ────────────────────────
        # 问题背景：call_model_node 的澄清检查依赖 `not plan` 条件，
        # 而 search_code 路由会在 call_model 之前由本节点生成计划，
        # 导致 `not plan` 为 False，澄清被跳过。
        # 解决方案：在此处提前拦截，返回空计划，
        # call_model 看到 `not plan=True` 后会自然触发澄清卡片。
        #
        # 有图片时不拦截：图片本身就是用户提供的视觉风格参考，无需再追问。
        from graph.nodes.call_model_node import _needs_webpage_clarification
        if not images and _needs_webpage_clarification(user_msg):
            logger.info(
                "Planner 网页澄清预检命中，返回空计划以触发澄清 | model=%s | user_msg=%.60s",
                model, user_msg,
            )
            return {
                "plan":               [],
                "current_step_index": 0,
                "step_iterations":    0,
            }

        llm = get_chat_llm(model=model, temperature=0.1)

        # ── 构建规划输入 ────────────────────────────────────────────────────
        # 优先使用 vision_description（已由 VisionNode 生成），
        # 避免此处重复调用视觉模型（原 planner 内联的 Ollama 调用已移至 VisionNode）
        if images:
            if vision_desc:
                planning_msg = (
                    f"[图片内容分析]\n{vision_desc}\n\n"
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
            {"role": "system", "content": _PLANNER_SYSTEM},
            {"role": "user",   "content": planning_msg},
        ]

        # ── 层 1：重试拿到非空 content ──────────────────────────────────────
        from logging_config import log_prompt
        log_prompt(state.get("conv_id", ""), "planner", model, messages)
        content = ""
        for attempt in range(3):
            completion = await llm.ainvoke(messages)
            raw = completion.choices[0].message.content or ""
            content = raw.strip()
            logger.info(
                "Planner 原始响应 [第%d次] | model=%s | len=%d | raw='%.200s'",
                attempt + 1, model, len(raw), raw,
            )
            if content:
                break
            logger.warning(
                "Planner 返回空内容 [第%d/3次] | model=%s",
                attempt + 1, model,
            )

        # ── 层 2：从响应中提取并解析 JSON ───────────────────────────────────
        plan_steps: list[PlanStep] = []
        try:
            if not content:
                raise ValueError("三次重试后仍返回空内容")

            # 去除 markdown code block
            if "```" in content:
                for part in content.split("```"):
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

            # 容错：模型有时在字符串值内用 ASCII 双引号包裹词语（如 "百业"），
            # 这会导致 JSON 解析失败。将 JSON 字符串值内的裸双引号替换为单引号。
            content = _fix_json_inner_quotes(content)

            logger.info("Planner JSON 提取后 | extracted='%.300s'", content)
            data = json.loads(content)

            for i, s in enumerate(data.get("steps", [])):
                plan_steps.append(PlanStep(
                    id=str(i + 1),
                    title=s.get("title", f"步骤 {i + 1}"),
                    description=s.get("description", ""),
                    status="pending",
                    result="",
                ))

            # 限制最多 _MAX_PLAN_STEPS 步
            plan_steps = plan_steps[:_MAX_PLAN_STEPS]
            logger.info(
                "Planner 解析成功 | steps=%d | titles=%s",
                len(plan_steps),
                [s["title"] for s in plan_steps],
            )

        except Exception as e:
            # ── 层 3：兜底单步 ──────────────────────────────────────────────
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
            "plan":               plan_steps,
            "current_step_index": 0,
            "step_iterations":    0,
        }
