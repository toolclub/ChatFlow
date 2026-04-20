"""
内置工具：请求用户澄清（取代原先通过 [NEED_CLARIFICATION] 文本标记传递的方案）

工作原理：
  1. 模型判断意图不明时调用 `request_clarification(question, items, note)`
  2. 工具把数据写入 contextvar `current_clarification`
  3. save_response_node 读取 contextvar，触发 `clarification_needed` SSE 事件
     并返回 `needs_clarification=True`，跳过本轮 assistant 回复的保存
  4. 前端渲染澄清卡片，等待用户回答

这样做的好处（对比旧方案 spec.md COMPAT 表第 2 行）：
  - 不再依赖模型输出特定文本标记（铁律 #1：不从模型文本推断状态）
  - 澄清结构化数据独立存放，不混入 content（铁律 #2）
  - system prompt 简短得多，模型更可控
"""

GUIDANCE = (
    "意图不明、风格/范围/技术栈等关键偏好缺失、或用户要生成网页/落地页/UI 界面/H5 却未指定设计方向时，"
    "必须调用 request_clarification，不得自行猜测后执行。"
    "items 使用 single_choice / multi_choice / text 三种 type，最多 4 项；"
    "每个选项提供 3~5 个可选项，覆盖最常见的用户选择。"
    "调用此工具后停止其他工具调用和回答，交由用户作答。"
)
ERROR_HINT = (
    "若工具调用失败，退而求其次用一句自然语言向用户简短提问；不要反复调用本工具。"
)
TAGS = ["meta", "clarification"]
DISPLAY_MODE = "hidden"  # 该工具产出由前端澄清卡片渲染，不需要显示 tool_result

import logging
from typing import Optional

from langchain_core.tools import tool

from sandbox.context import current_clarification

logger = logging.getLogger("tools.clarification")

_ALLOWED_ITEM_TYPES = {"single_choice", "multi_choice", "text"}
_MAX_ITEMS = 4
_MAX_OPTIONS = 8
_MAX_QUESTION_LEN = 200
_MAX_LABEL_LEN = 80
_MAX_OPTION_LEN = 60


def _normalize_items(items: list) -> list[dict]:
    """
    规范化 items：
      - 过滤非法 type / 缺字段
      - 截断过长文本
      - 保留最多 _MAX_ITEMS 项
    """
    cleaned: list[dict] = []
    if not isinstance(items, list):
        return cleaned
    for raw in items[:_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("type") or "").strip().lower()
        if item_type not in _ALLOWED_ITEM_TYPES:
            continue
        item_id = str(raw.get("id") or "").strip() or f"field_{len(cleaned) + 1}"
        label = str(raw.get("label") or "").strip()[:_MAX_LABEL_LEN]
        if not label:
            continue
        entry: dict = {"id": item_id, "type": item_type, "label": label}
        if item_type in ("single_choice", "multi_choice"):
            raw_options = raw.get("options") or []
            if not isinstance(raw_options, list):
                continue
            options: list[str] = []
            for opt in raw_options[:_MAX_OPTIONS]:
                text = str(opt).strip()[:_MAX_OPTION_LEN]
                if text:
                    options.append(text)
            if len(options) < 2:
                # 单/多选至少 2 个选项才有意义
                continue
            entry["options"] = options
        else:
            # text：可选 placeholder
            placeholder = raw.get("placeholder")
            if placeholder:
                entry["placeholder"] = str(placeholder).strip()[:_MAX_LABEL_LEN]
        cleaned.append(entry)
    return cleaned


@tool
async def request_clarification(
    question: str,
    items: list,
    note: Optional[str] = None,
) -> str:
    """
    向用户发起澄清问询，用于意图模糊时收集关键信息。

    只在以下情况调用：
      - 用户意图存在多种合理解读
      - 风格 / 范围 / 技术栈等关键偏好缺失
      - 用户要生成网页、落地页、UI、H5 但未指定设计方向

    Args:
        question: 一句话说明要问什么（最多 200 字，中文）。
        items:    澄清项列表，最多 4 项。每项必须是 dict，字段：
                  - id      : 字段名（英文标识符）
                  - type    : single_choice | multi_choice | text
                  - label   : 问题描述
                  - options : 选项列表（single/multi_choice 必填，至少 2 项）
                  - placeholder : 占位符（text 类型可选）
        note:     额外可见说明（可选，最多 200 字）。

    Returns:
        执行结果说明（该说明不会展示给用户，仅用于模型自我确认）。
    """
    q = (question or "").strip()[:_MAX_QUESTION_LEN]
    if not q:
        return "⚠️ question 不能为空，未生成澄清卡片。"

    cleaned_items = _normalize_items(items or [])
    if not cleaned_items:
        return "⚠️ items 为空或全部非法，未生成澄清卡片。请至少提供一个合法澄清项。"

    payload: dict = {"question": q, "items": cleaned_items}
    if note:
        note_text = str(note).strip()[:_MAX_QUESTION_LEN]
        if note_text:
            payload["note"] = note_text

    # 关键：对 contextvar 中的 dict 做 in-place 变更，不能 .set() 新对象，
    # 否则 ToolNode 所在 Task 的修改不会回写到父 Task（save_response_node）。
    slot = current_clarification.get()
    if slot is None:
        # 极端兜底：stream.py 未初始化（非 HTTP 路径），这里也不抛异常
        logger.warning("request_clarification 未找到 clarification_slot，可能上下文未初始化。")
        return "⚠️ 澄清槽未初始化，本次无法生成卡片。"
    slot["data"] = payload
    logger.info(
        "request_clarification 已写入澄清槽 | question=%.60s | items=%d",
        q, len(cleaned_items),
    )
    return "✅ 澄清卡片已生成，请停止其他工具调用和输出，等待用户作答。"
