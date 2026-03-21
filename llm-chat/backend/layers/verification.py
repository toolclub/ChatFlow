"""
第 8 层 – Verification（验证）
日志记录与可观测性辅助工具。
author: leizihao
email: lzh19162600626@gmail.com
"""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("harness")


def log_chat(conv_id: str, model: str) -> None:
    logger.info("[聊天] conv=%s  model=%s", conv_id, model)


def log_compression(conv_id: str, compressed: int, kept: int, summary_len: int) -> None:
    logger.info(
        "[记忆压缩] conv=%s  compressed=%d  kept=%d  summary_len=%d",
        conv_id, compressed, kept, summary_len,
    )


def log_error(msg: str, exc: Exception = None) -> None:
    logger.error("%s  exc=%s", msg, exc)
