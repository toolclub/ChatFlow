"""
内置工具：核心记忆写入（显式）

仅在用户明确要求"以后默认 / 记住一下 / 我们项目规定 / 我是 X 角色"时调用。
不做任何隐式抽取——不会从用户消息里猜测偏好。
"""

GUIDANCE = (
    "这是「签合同」，不是「记笔记」。"
    "只在用户明确说「以后默认 / 记住一下 / 我们约定 / 我是 X 角色 / 项目规定」等长期生效的话时召唤。"
    "一次只记一条，只记用户原话的核心含义——不从闲聊里推断，不用于临时话题或普通问答。"
)
ERROR_HINT = "若写入失败，跳过即可，不要反复重试。"
TAGS = ["memory", "utility"]
DISPLAY_MODE = "default"

import logging

from langchain_core.tools import tool

from memory import store as memory_store
from memory.core_memory import VALID_CATEGORIES, add_to_core_memory
from sandbox.context import current_conv_id

logger = logging.getLogger("tools.remember")

_MAX_CONTENT_LEN = 200


@tool
async def remember_preference(category: str, content: str) -> str:
    """
    把一条用户**显式**表达的长期信息立字成约——这是"签合同"，不是"记笔记"。

    何时召唤：用户明确说"记住 / 以后默认 / 我们约定 / 我是 X 角色 / 项目规定"等长期生效的话。
    何时不召唤：临时问答、一次性查询结果、你自己的推测——这些都不属于长期记忆。

    Args:
        category: 分类，必须是以下之一：
                  - "user_profile"        用户身份/角色（示例："我是前端工程师"）
                  - "project_rules"       项目规范/硬性约束（示例："禁止提交 console.log"）
                  - "learned_preferences" 用户偏好（示例："默认用中文回答"）
                  - "current_task"        当前正在进行的长期任务（单条，新值覆盖旧值）
        content:  要记住的完整内容，一句话概括，不超过 200 字。

    Returns:
        执行结果说明。
    """
    conv_id = current_conv_id.get()
    if not conv_id:
        return "⚠️ 无法确定当前会话，本次记忆写入已跳过。"

    if category not in VALID_CATEGORIES:
        allowed = ", ".join(VALID_CATEGORIES)
        return f"⚠️ 非法的 category: {category}，允许值: {allowed}"

    clean = (content or "").strip()
    if not clean:
        return "⚠️ content 为空，未写入。"
    if len(clean) > _MAX_CONTENT_LEN:
        return f"⚠️ content 超过 {_MAX_CONTENT_LEN} 字，请精简到一句话核心内容。"

    conv = memory_store.get(conv_id)
    if not conv:
        return "⚠️ 未找到当前会话对象，跳过。"

    try:
        changed = add_to_core_memory(conv, category, clean)
    except ValueError as exc:
        return f"⚠️ {exc}"

    if not changed:
        return f"ℹ️ 核心记忆已包含相同内容，未重复写入（category={category}）。"

    await memory_store.save(conv)
    logger.info(
        "core_memory 显式写入 | conv=%s | category=%s | len=%d",
        conv_id, category, len(clean),
    )
    return f"✅ 已记住（{category}）：{clean}"
