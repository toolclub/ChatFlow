"""
上下文构建器：将对话历史组装为发送给 LLM 的 LangChain 消息列表

分层系统提示（按优先级从高到低排列，LLM 从上到下读取）：
  1. 平台身份 + 全局规则（来自 DEFAULT_SYSTEM_PROMPT + 当前日期）
  2. 项目规则（core_memory.project_rules —— 硬约束）
  3. 用户画像（core_memory.user_profile —— 长期属性）
  4. 已确认偏好（core_memory.learned_preferences —— 表达方式 / 默认选择）
  5. 当前任务（core_memory.current_task —— 会话级临时目标）
  6. 对话背景摘要（mid_term_summary —— 远期对话的语义压缩）
  7. 长期记忆（RAG 检索 —— 相似历史对话片段）
  8. 可用工具指南（放在最底层，避免与规则层争抢注意力）

设计理由：
  - 把"规则 / 偏好 / 任务"从运行时对话历史中解耦，在规则层就能看到，
    不必靠工具来检索；forget_mode 下也始终保留。
  - 分层清晰后，调试时能一眼看出"模型拿到什么信息",便于追查不服从指令的原因。

改进点（相比早先版本）：
  1. 长期记忆去重：过滤与近期对话高度重叠的记忆，减少噪音
  2. 渐进式 forget_mode：不是二元的"全要/只要几条"，而是梯度截断
  3. 结构化系统提示：明确分层，LLM 更容易建立指令优先级
"""
from __future__ import annotations

from datetime import date

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from config import SHORT_TERM_FORGET_TURNS, SHORT_TERM_MAX_TURNS, DEFAULT_SYSTEM_PROMPT
from memory.core_memory import ensure_core_memory
from memory.schema import Conversation, Message

_MAX_CORE_ITEMS_RENDER = 8  # 每个 core_memory list 字段最多渲染多少条


def build_messages(
    conv: Conversation | None,
    long_term_memories: list[str] | None = None,
    forget_mode: bool = False,
    tool_names: list[str] | None = None,  # 保留参数兼容旧调用；当前由 get_tools_guidance 内部决定
    route: str = "",
    quant_snapshots_text: list[str] | None = None,
) -> list[BaseMessage]:
    """
    构建发送给 LLM 的完整消息列表。

    Args:
        conv:               当前对话对象（为 None 时使用默认提示词）
        long_term_memories: 从 Qdrant 检索到的相关历史记录
        forget_mode:        True 时缩短历史窗口，且不注入中期摘要 / 长期记忆（降低干扰），
                            但 core_memory 中的规则/偏好/画像仍然保留（这些是显式写入的长期事实）
        tool_names:         兼容保留，未使用
        route:              当前路由，用于裁剪无关工具 guidance
        quant_snapshots_text: 附加的量化快照上下文

    Returns:
        list[BaseMessage]，可直接传给 LLM.ainvoke()
    """
    del tool_names  # 参数保留是为了不破坏旧调用位置，实际已由 guidance 层自行决定

    today = date.today().strftime("%Y年%m月%d日")
    base_prompt = (conv.system_prompt if conv and conv.system_prompt else DEFAULT_SYSTEM_PROMPT)
    core = ensure_core_memory(getattr(conv, "core_memory", {}) if conv else {})

    layers: list[str] = []

    # Layer 1: 平台身份 + 全局规则
    layers.append(
        f"{base_prompt}\n\n当前日期：{today}。搜索时直接用核心关键词，不要手动添加年份。"
    )

    # Layer 1.5: 路由专用约束（如有 prompts/routes/{route}.md 文件就注入）
    if route:
        try:
            from prompts import load_prompt
            route_prompt = load_prompt(f"routes/{route}")
            if route_prompt:
                layers.append(route_prompt)
        except FileNotFoundError:
            pass  # 该路由无专用 prompt，跳过

    # Layer 2: 项目规则（硬约束 —— 用户显式声明的项目级约束）
    if core["project_rules"]:
        layers.append(_format_list("项目规则（硬约束，优先遵守）", core["project_rules"]))

    # Layer 3: 用户画像（长期属性）
    if core["user_profile"]:
        layers.append(_format_list("用户画像", core["user_profile"]))

    # Layer 4: 已确认偏好（表达 / 风格 / 默认选择）
    if core["learned_preferences"]:
        layers.append(_format_list("已确认偏好", core["learned_preferences"]))

    # Layer 5: 当前任务（会话级临时目标）
    if core["current_task"]:
        layers.append(f"【当前任务】\n- {core['current_task']}")
        
    # Layer 5.5: 量化选股快照上下文
    if quant_snapshots_text:
        layers.append("\n\n".join(quant_snapshots_text))

    # Layer 6 + 7: 背景摘要 + 长期记忆（forget_mode 下跳过）
    if not forget_mode:
        if conv and conv.mid_term_summary:
            layers.append(
                "【对话背景摘要】\n"
                "以下是之前对话的压缩摘要，请结合这些背景来回答：\n"
                f"{conv.mid_term_summary}"
            )

        cleaned_memories = _deduplicate_memories(
            long_term_memories or [],
            conv.messages if conv else [],
        )
        if cleaned_memories:
            memories_text = "\n\n".join(cleaned_memories)
            layers.append(
                "【长期记忆】\n"
                "以下是与当前问题高度相关的历史对话记录，请参考这些内容来回答：\n"
                f"{memories_text}"
            )

    # Layer 8: 可用工具指南（放最底层，规则优先级不被工具提示抢走）
    try:
        from tools import get_tools_guidance
        guidance = get_tools_guidance(route=route)
        if guidance:
            layers.append(guidance)
    except Exception:
        pass

    messages: list[BaseMessage] = [SystemMessage(content="\n\n".join(layers))]

    # ── 滑动窗口历史 ───────────────────────────────────────────────────────────
    if conv and conv.messages:
        window = _select_history_window(conv.messages, forget_mode)
        for msg in window:
            if msg.role == "user":
                messages.append(HumanMessage(content=msg.content))
            elif msg.role == "assistant":
                # 截断过长的 AI 历史回复（多步执行结果可能很长）
                content = _truncate_assistant_history(msg.content)
                # DB-first：tool_summary 在独立字段，按需附加到 LLM 上下文
                # 压缩后 tool_summary 已清空，不会重复注入
                if msg.tool_summary:
                    content += "\n\n[工具调用摘要] " + msg.tool_summary[:300]
                
                # DeepSeek thinking 模式适配：保留 reasoning_content 和 tool_calls
                # 使用 additional_kwargs 暂存，供 BaseNode._to_openai_messages 提取
                additional_kwargs = {}
                if msg.thinking:
                    additional_kwargs["reasoning_content"] = msg.thinking
                
                # 恢复消息详情中的工具调用，确保 MiniMax/DeepSeek 等模型重放上下文一致
                tool_calls = None
                try:
                    # MessageDetailStore 加载的消息详情存放在额外的 metadata 或后续补充逻辑中
                    # 这里假设 schema.Message 已经通过 memory/store.py 加载了这些属性
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        tool_calls = msg.tool_calls
                except Exception:
                    pass

                messages.append(AIMessage(
                    content=content, 
                    additional_kwargs=additional_kwargs,
                    tool_calls=tool_calls or []
                ))

    return messages


def _format_list(title: str, items: list[str]) -> str:
    """把 core_memory list 字段渲染成带标题的要点列表。"""
    shown = items[-_MAX_CORE_ITEMS_RENDER:]
    body = "\n".join(f"- {item}" for item in shown)
    return f"【{title}】\n{body}"


def _select_history_window(messages: list[Message], forget_mode: bool) -> list[Message]:
    """
    选择历史消息窗口。

    正常模式：最近 SHORT_TERM_MAX_TURNS 轮（取 *2 条，含 user+assistant）
    forget_mode：缩减到 SHORT_TERM_FORGET_TURNS 轮，减少干扰

    渐进式策略：forget_mode 不是硬截断到 0，而是给一个中间值，
    保留最近几轮以维持基本对话连贯性。
    """
    max_msgs = (SHORT_TERM_MAX_TURNS * 2) if not forget_mode else (SHORT_TERM_FORGET_TURNS * 2)
    return messages[-max_msgs:]


def _truncate_assistant_history(content: str, max_len: int = 800) -> str:
    """
    截断历史 AI 回复中过长的内容。

    新数据：step_summary 已在独立字段，content 是纯文本，直接截断。
    旧数据（COMPAT）：可能在 content 中嵌入了"执行过程摘要"标记，截断时优先去除。
    """
    if len(content) <= max_len:
        return content

    # COMPAT: 旧数据可能在 content 中嵌入了执行过程摘要标记
    # 新数据不会包含此标记（已分离到 step_summary 独立字段）。
    # 当所有旧消息完成压缩后可移除此兼容逻辑。
    summary_marker = "【执行过程摘要】"
    if summary_marker in content:
        idx = content.index(summary_marker)
        core = content[:idx].strip()
        return core[:max_len] + ("..." if len(core) > max_len else "")

    return content[:max_len] + "..."


def _deduplicate_memories(
    long_term_memories: list[str],
    recent_messages: list[Message],
) -> list[str]:
    """
    过滤与近期对话内容高度重叠的长期记忆，避免重复注入。

    策略：提取近期消息的关键词集合，若记忆内容的关键词重叠率超过阈值，则跳过。
    简单且高效，不引入额外 embedding 调用。
    """
    if not long_term_memories:
        return []

    # 构建近期对话的关键词集合（最近 6 条消息）
    recent_text = " ".join(
        m.content for m in recent_messages[-6:]
    ).lower()
    recent_words = set(w for w in recent_text.split() if len(w) > 1)

    result: list[str] = []
    for mem in long_term_memories:
        mem_words = [w for w in mem.lower().split() if len(w) > 1]
        if len(mem_words) < 5:
            # 极短记忆直接保留，避免误删
            result.append(mem)
            continue

        overlap = sum(1 for w in mem_words if w in recent_words) / len(mem_words)
        if overlap >= 0.55:
            # 超过 55% 关键词已在近期对话中出现 → 记忆为冗余信息，跳过
            continue
        result.append(mem)

    return result


def should_compress(conv: Conversation) -> bool:
    """当未摘要的消息累积到阈值时返回 True。"""
    from config import COMPRESS_TRIGGER
    unsummarised = len(conv.messages) - conv.mid_term_cursor
    return unsummarised >= COMPRESS_TRIGGER * 2


def slice_for_compression(conv: Conversation):
    """
    返回 (待摘要消息列表, 新游标位置)。
    只将游标到滑动窗口起点之间的消息发送给摘要模型——保持滑动窗口完整。
    """
    keep_count = (SHORT_TERM_MAX_TURNS // 2) * 2
    new_cursor = max(conv.mid_term_cursor, len(conv.messages) - keep_count)
    to_summarise = conv.messages[conv.mid_term_cursor:new_cursor]
    return to_summarise, new_cursor
