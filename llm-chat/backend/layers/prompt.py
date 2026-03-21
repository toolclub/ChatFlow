"""
第 1 层 – Prompt（提示）
系统提示、人格设定及提示模板构建工具。
author: leizihao
email: lzh19162600626@gmail.com
"""
from config import DEFAULT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT, MAX_SUMMARY_LENGTH


def ensure_system_prompt(raw: str) -> str:
    """返回原始提示，若为空则回退到默认提示。"""
    return raw.strip() if raw and raw.strip() else DEFAULT_SYSTEM_PROMPT


def build_summary_messages(history_text: str, existing_summary: str) -> list[dict]:
    """构建发送给摘要模型的消息列表。"""
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
