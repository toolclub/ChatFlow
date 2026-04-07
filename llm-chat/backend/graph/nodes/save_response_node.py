"""
SaveResponseNode：保存响应节点

职责：
  - 将本轮用户消息和 AI 最终回复追加到 ConversationStore 并持久化
  - 含图片时生成描述占位符，避免将原始 base64 存入数据库
  - 保存工具调用事件到 tool_events 表（供前端历史查看）
  - 写回语义缓存（chat/code 永不过期；search/search_code 带 TTL）
  - 清理工具调用残留文本（MiniMax 等模型在流式模式下可能输出 function call 文本）
"""
import json
import logging
import re

from langchain_core.callbacks.manager import adispatch_custom_event

from config import (
    DEFAULT_SYSTEM_PROMPT,
    SEMANTIC_CACHE_NAMESPACE_MODE,
    SEMANTIC_CACHE_SEARCH_TTL_HOURS,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
)
from graph.nodes.base import BaseNode
from graph.state import GraphState
from memory import store as memory_store

logger = logging.getLogger("graph.nodes.save_response")

# 工具调用残留文本标识符（MiniMax 等模型特有）
_TOOL_CALL_ARTIFACTS = ("[TOOL_CALL]", "minimax:tool_call", "<tool_call>", "[/TOOL_CALL]")


class SaveResponseNode(BaseNode):
    """保存响应节点：将本轮对话持久化并更新语义缓存。"""

    @property
    def name(self) -> str:
        return "save_response"

    async def execute(self, state: GraphState) -> dict:
        """
        持久化流程：
          1. 提取工具调用事件列表
          2. 处理图片占位符（含图片时生成描述替代 base64）
          3. 写入用户消息到数据库
          4. 清理并写入 AI 回复到数据库（含工具调用摘要）
          5. 保存工具调用事件
          6. 写回语义缓存
        """
        from logging_config import get_conv_logger

        conv_id   = state["conv_id"]
        client_id = state.get("client_id", "")
        user_msg  = state["user_message"]
        images    = state.get("images", [])

        # 原始 full_response（含 think 块），用于澄清标记检测
        raw_response  = state.get("full_response", "")
        # 移除 think 块后的 full_response（用于保存和日志）
        full_response = self._strip_think_blocks(raw_response)

        # 对话链路日志
        clog = get_conv_logger(client_id, conv_id)
        route           = state.get("route", "chat")
        plan            = state.get("plan", [])
        tool_events_list = self._extract_tool_events(state)
        tool_names_list  = [ev["tool_name"] for ev in tool_events_list]
        clog.info(
            "对话完成 | route=%s | model=%s | plan_steps=%d | tools=%s | "
            "response_len=%d | images=%d | user_msg=%.60s",
            route,
            state.get("answer_model", state.get("model", "")),
            len(plan),
            tool_names_list,
            len(full_response),
            len(images),
            user_msg,
        )

        # ── 图片处理：生成存储用消息（优先用完整视觉描述，降级用短占位符） ────
        # 提前到澄清检测之前，确保澄清时也能保存图片上下文到历史
        user_msg_to_save = await self._build_user_msg_for_storage(state)

        # ── 澄清检测：同时检查原始（含 think 块）和去除后的文本 ──────────────
        # 原因：qwen3 等模型有时会把 [NEED_CLARIFICATION] 输出在 <think> 块内部，
        # 去除 think 块后标记消失。两者都检测，取先找到的结果。
        clar_data = None
        for candidate in (full_response, raw_response):
            if candidate and self._is_clarification_request(candidate):
                clar_data = self._extract_clarification_data(candidate)
                if clar_data:
                    break

        if clar_data:
            await adispatch_custom_event("clarification_needed", clar_data)
            logger.info(
                "澄清问询 | conv=%s | items=%d | question=%.80s",
                conv_id, len(clar_data.get("items", [])), clar_data.get("question", ""),
            )
            # 保存用户消息（含图片描述），下一轮才能从历史中恢复上下文
            await memory_store.add_message(conv_id, "user", self._sanitize_for_db(user_msg_to_save))
            return {"needs_clarification": True}

        # ── 写入用户消息 ────────────────────────────────────────────────────
        await memory_store.add_message(conv_id, "user", self._sanitize_for_db(user_msg_to_save))

        # ── 写入 AI 回复（含多步摘要 + 工具调用摘要） ───────────────────────────
        # 多步计划：将所有步骤结果保存，下次对话时模型能看到完整执行过程
        if full_response:
            tool_summary    = self._build_tool_summary(state)
            step_context    = self._build_step_context(state)
            parts           = [full_response]
            if step_context:
                parts.append(step_context)
            if tool_summary:
                parts.append(tool_summary)
            content_to_save = "\n\n".join(parts)
            await memory_store.add_message(
                conv_id, "assistant", self._sanitize_for_db(content_to_save)
            )

        # ── 保存工具调用事件 ─────────────────────────────────────────────────
        if tool_events_list:
            from memory.tool_events import save_tool_event
            for ev in tool_events_list:
                await save_tool_event(conv_id, ev["tool_name"], ev["tool_input"])

        # ── 写回语义缓存 ─────────────────────────────────────────────────────
        await self._write_cache(state, user_msg, full_response, route, client_id, conv_id)

        # ── 确保 DB 中所有步骤都标记为 done（兜底） ────────────────────────────
        # reflector 每步完成时会写 DB，但最后一步可能因异常/时序未更新
        plan_id = state.get("plan_id", "")
        plan = state.get("plan", [])
        if plan_id and plan:
            try:
                from db.plan_store import finalize_all_steps
                await finalize_all_steps(plan_id, plan)
            except Exception:
                pass  # 不影响主流程

        return {}

    # ══════════════════════════════════════════════════════════════════════════
    # 私有工具方法
    # ══════════════════════════════════════════════════════════════════════════

    async def _write_cache(
        self,
        state: GraphState,
        user_msg: str,
        full_response: str,
        route: str,
        client_id: str,
        conv_id: str,
    ) -> None:
        """
        将本轮对话写入语义缓存。

        不缓存的情形：
          - 已命中缓存（避免覆盖）
          - 含图片（语义随图片内容变化）
          - 含工具调用残留文本（脏数据）
        """
        if not full_response:
            return
        if state.get("cache_hit"):
            return
        if state.get("images"):
            return

        # 检测并清理工具调用残留文本
        has_artifact = any(a in full_response for a in _TOOL_CALL_ARTIFACTS)
        if has_artifact:
            cleaned = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", full_response, flags=re.DOTALL)
            cleaned = re.sub(r"minimax:tool_call.*", "", cleaned, flags=re.DOTALL)
            cleaned = re.sub(r"<tool_call>.*?</tool_call>", "", cleaned, flags=re.DOTALL)
            cleaned = cleaned.strip()
            if cleaned:
                logger.warning(
                    "ARTIFACT CLEAN | 工具调用残留文本已清理后写入缓存 | response='%.100s'",
                    full_response,
                )
                full_response = cleaned
            else:
                logger.warning(
                    "ARTIFACT SKIP | 响应清理后为空，跳过缓存 | response='%.100s'",
                    full_response,
                )
                return

        try:
            from cache.factory import get_cache
            from graph.nodes.cache_node import SemanticCacheNode

            cache     = get_cache()
            conv      = memory_store.get(conv_id)
            namespace = SemanticCacheNode._derive_cache_namespace(
                conv, SEMANTIC_CACHE_NAMESPACE_MODE, client_id
            )
            # search 类路由带 TTL（实时数据有时效性）；chat/code 永不过期
            ttl = (
                SEMANTIC_CACHE_SEARCH_TTL_HOURS * 3600
                if route in ("search", "search_code")
                else None
            )
            await cache.store(user_msg, full_response, namespace, ttl_seconds=ttl)
        except Exception as exc:
            logger.warning("写入语义缓存失败（不影响主流程）: %s", exc)

    async def _build_user_msg_for_storage(self, state: GraphState) -> str:
        """
        构建用于持久化到 DB 的用户消息。

        - 有图片且 VisionNode 已生成完整描述（vision_description）：
            将完整描述附加到用户消息，保证下一轮（如澄清后）能从历史中恢复图片上下文。
        - 有图片但 vision_description 为空（VisionNode 降级）：
            调用视觉模型生成简短占位描述后附加。
        - 无图片：直接返回用户消息原文。
        """
        user_msg = state["user_message"]
        images   = state.get("images", [])

        if not images:
            return user_msg

        vision_description = state.get("vision_description", "")
        if vision_description:
            # 有完整视觉分析时直接附加（足够详细，供下一轮上下文）
            combined = f"{user_msg}\n[图片内容分析]\n{vision_description}" if user_msg.strip() else vision_description
        else:
            # 降级：生成简短占位描述
            model_for_desc = state.get("answer_model") or state["model"]
            img_desc       = await self._describe_images_for_storage(images, model_for_desc)
            placeholder    = f"[用户上传了图片：图片内容大致为{img_desc}]"
            combined       = f"{user_msg}\n{placeholder}" if user_msg.strip() else placeholder

        return combined

    @staticmethod
    async def _describe_images_for_storage(images: list[str], model: str) -> str:
        """
        用视觉 LLM 生成图片内容的简短描述，替代存储/记忆中的原始 base64 数据。
        失败时静默降级，返回通用占位文本。
        """
        try:
            from openai import AsyncOpenAI

            vision_model = VISION_MODEL or model
            content: list = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                    },
                }
                for img in images
            ]
            content.append({
                "type": "text",
                "text": "请简短描述图片的主要内容，不超过50个字，直接描述，不要解释。",
            })

            client = AsyncOpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
            resp   = await client.chat.completions.create(
                model=vision_model,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
            )
            return (resp.choices[0].message.content or "").strip() or "图片内容"
        except Exception as exc:
            logger.warning("图片描述生成失败: %s", exc)
            return "图片内容"

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        """移除 <think>...</think> 推理块（qwen3 等模型的思考内容不应存入上下文）。"""
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    @staticmethod
    def _sanitize_for_db(text: str) -> str:
        """移除 PostgreSQL UTF-8 不支持的字符（null 字节等），防止存库失败。"""
        return text.replace("\x00", "").replace("\u0000", "")

    @staticmethod
    def _extract_tool_events(state: GraphState) -> list[dict]:
        """从 messages 中提取工具调用事件列表（用于持久化到 tool_events 表）。"""
        messages = list(state.get("messages", []))
        events   = []
        for m in messages:
            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    if name:
                        events.append({"tool_name": name, "tool_input": args or {}})
        return events

    @staticmethod
    def _build_step_context(state: GraphState) -> str:
        """
        为多步计划构建执行过程摘要，保存到 DB 作为下次对话的上下文。

        只有在计划步骤数 > 1 且有中间步骤结果时才生成，避免单步任务冗余。
        """
        step_results = list(state.get("step_results") or [])
        plan         = state.get("plan", [])

        # 无多步结果或单步任务不生成摘要
        if len(step_results) <= 1 or len(plan) <= 1:
            return ""

        # 中间步骤摘要（不含最后一步，因为最后一步就是 full_response）
        lines: list[str] = []
        for i, result in enumerate(step_results[:-1]):
            title = plan[i]["title"] if i < len(plan) else f"步骤{i + 1}"
            short = result[:200] + ("..." if len(result) > 200 else "")
            lines.append(f"- 步骤{i + 1}（{title}）：{short}")

        if not lines:
            return ""

        return "【执行过程摘要】\n" + "\n".join(lines)

    @staticmethod
    def _build_tool_summary(state: GraphState) -> str:
        """从 messages 中提取工具调用摘要，追加到 AI 回复尾部用于上下文持久化。"""
        messages  = list(state.get("messages", []))
        summaries = []
        for m in messages:
            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    summaries.append(
                        f"- 调用工具: {name}({json.dumps(args, ensure_ascii=False)[:200]})"
                    )
            # ToolMessage：记录工具返回内容摘要
            if type(m).__name__ == "ToolMessage":
                content = str(m.content)[:300]
                summaries.append(f"  结果: {content}")

        if summaries:
            return "【工具调用记录】\n" + "\n".join(summaries[:20])  # 最多 20 条
        return ""

    # ── 澄清检测与提取 ───────────────────────────────────────────────────────

    _CLAR_START = "[NEED_CLARIFICATION]"
    _CLAR_END   = "[/NEED_CLARIFICATION]"

    @classmethod
    def _is_clarification_request(cls, text: str) -> bool:
        """检测模型回复中是否含有澄清开始标记（不强求闭合标签）。"""
        return cls._CLAR_START in text

    @classmethod
    def _extract_clarification_data(cls, text: str) -> dict | None:
        """
        从标记中提取并解析 JSON，容错处理以下情况：
          1. 有完整标记：[NEED_CLARIFICATION]{...}[/NEED_CLARIFICATION]
          2. 只有开始标记：[NEED_CLARIFICATION]{...}
          3. JSON 内嵌在 think 块中，已由调用方传入原始文本
          4. 模型输出了多余文本，JSON 混在其中
          5. 模型将 items 写成多个独立 JSON 对象（items 数组提前闭合）

        返回解析后的 dict（含 question + items），否则返回 None。
        """
        start = text.find(cls._CLAR_START)
        if start == -1:
            return None

        after_start = text[start + len(cls._CLAR_START):]

        # 优先：有闭合标签，取中间内容
        end_in_after = after_start.find(cls._CLAR_END)
        raw_candidate = after_start[:end_in_after].strip() if end_in_after != -1 else after_start.strip()

        # 尝试直接解析
        data = cls._try_parse_json(raw_candidate)
        if data:
            return data

        # 容错 1：从候选文本中用正则提取贪婪匹配的完整 JSON 对象
        json_match = re.search(r'\{[\s\S]*\}', raw_candidate)
        if json_match:
            data = cls._try_parse_json(json_match.group())
            if data:
                return data

        # 容错 2：模型把 items 写成多个独立顶层对象
        # 例：{"question":"...", "items":[{item1}]},{item2},{item3}
        data = cls._try_reassemble_fragmented_items(raw_candidate)
        if data:
            return data

        logger.warning(
            "澄清 JSON 解析失败，降级为普通回复 | raw=%.200s", raw_candidate
        )
        return None

    @staticmethod
    def _try_reassemble_fragmented_items(raw: str) -> dict | None:
        """
        处理模型把 items 数组元素写成独立顶层对象的情况：
          {"question":"...", "items":[{item1}]},{item2},{item3}
        → {"question":"...", "items":[{item1},{item2},{item3}]}

        策略：顺序扫描 raw 中所有合法 JSON 对象；
              第一个含 question 的对象为主体，其余含 id 的对象追加进 items。
        """
        objects: list[dict] = []
        i = 0
        while i < len(raw):
            if raw[i] != '{':
                i += 1
                continue
            depth, j = 0, i
            while j < len(raw):
                if raw[j] == '{':
                    depth += 1
                elif raw[j] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(raw[i:j + 1])
                            if isinstance(obj, dict):
                                objects.append(obj)
                        except (json.JSONDecodeError, ValueError):
                            pass
                        i = j + 1
                        break
                j += 1
            else:
                break

        if not objects:
            return None

        main = objects[0]
        if not isinstance(main, dict) or "question" not in main:
            return None

        if not isinstance(main.get("items"), list):
            main["items"] = []

        # 额外的 item 对象追加进 items 数组
        for extra in objects[1:]:
            if isinstance(extra, dict) and "id" in extra:
                main["items"].append(extra)

        if main.get("question") and isinstance(main.get("items"), list) and main["items"]:
            return main
        return None

    @staticmethod
    def _try_parse_json(raw: str) -> dict | None:
        """尝试解析 JSON，校验必须含 question + items 列表。"""
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "question" in data and isinstance(data.get("items"), list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None
