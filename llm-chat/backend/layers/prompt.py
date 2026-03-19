"""
Layer 1 – Prompt
System prompts, personas, and prompt-template builders.
"""
from config import DEFAULT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT, MAX_SUMMARY_LENGTH


def ensure_system_prompt(raw: str) -> str:
    """Return raw prompt or fall back to the default."""
    return raw.strip() if raw and raw.strip() else DEFAULT_SYSTEM_PROMPT


def build_summary_messages(history_text: str, existing_summary: str) -> list[dict]:
    """Build the message list sent to the summary model."""
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"请将以下对话历史压缩成摘要。\n\n"
                f"已有的历史摘要：\n{existing_summary or '（无）'}\n\n"
                f"新增对话：\n{history_text}\n\n"
                f"请输出更新后的综合摘要（不超过{MAX_SUMMARY_LENGTH}字）："
            ),
        },
    ]
