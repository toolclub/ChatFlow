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
            return {"needs_clarification": True}

        # ── 图片处理：生成描述占位符 ─────────────────────────────────────────
        if images:
            model_for_desc   = state.get("answer_model") or state["model"]
            img_desc         = await self._describe_images_for_storage(images, model_for_desc)
            placeholder      = f"[用户上传了图片：图片内容大致为{img_desc}]"
            user_msg_to_save = f"{user_msg}\n{placeholder}" if user_msg.strip() else placeholder
        else:
            user_msg_to_save = user_msg

        # ── 写入用户消息 ────────────────────────────────────────────────────
        await memory_store.add_message(conv_id, "user", self._sanitize_for_db(user_msg_to_save))

        # ── 写入 AI 回复（含工具调用摘要） ──────────────────────────────────
        if full_response:
            tool_summary     = self._build_tool_summary(state)
            content_to_save  = (
                full_response + "\n\n" + tool_summary
                if tool_summary
                else full_response
            )
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

        # 容错：从候选文本中用正则提取第一个完整 JSON 对象
        json_match = re.search(r'\{[\s\S]*\}', raw_candidate)
        if json_match:
            data = cls._try_parse_json(json_match.group())
            if data:
                return data

        logger.warning(
            "澄清 JSON 解析失败，降级为普通回复 | raw=%.200s", raw_candidate
        )
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
