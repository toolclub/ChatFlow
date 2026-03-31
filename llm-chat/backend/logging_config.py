"""
日志配置模块：支持全局日志 + 按 client_id/conv_id 分文件记录

文件结构：
  {LOG_DIR}/chatflow.log              ← 全局日志（所有请求）
  {LOG_DIR}/{client_id}/{conv_id}.log ← 每次对话的完整链路日志
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 避免循环导入：LOG_DIR 在 setup_logging() 调用时从 config 获取
_log_dir: Path | None = None
_conv_loggers: dict[str, logging.Logger] = {}


def setup_logging(log_dir: str) -> None:
    """
    初始化全局日志：控制台 + 主日志文件。
    应在应用启动时（lifespan）调用一次。
    """
    global _log_dir
    _log_dir = Path(log_dir)
    _log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 避免重复添加 handler（reload 场景）
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 全局文件（20 MB × 5 个备份）
    fh = RotatingFileHandler(
        _log_dir / "chatflow.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_conv_logger(client_id: str, conv_id: str) -> logging.Logger:
    """
    获取对话级日志记录器。
    日志写入 {LOG_DIR}/{client_id}/{conv_id}.log，同时传播到根日志。
    """
    if _log_dir is None:
        return logging.getLogger(f"conv.{conv_id}")

    key = f"{client_id}:{conv_id}"
    if key in _conv_loggers:
        return _conv_loggers[key]

    cid = (client_id or "anonymous")[:36]
    client_dir = _log_dir / cid
    client_dir.mkdir(parents=True, exist_ok=True)

    name = f"conv.{cid[:8]}.{conv_id}"
    logger = logging.getLogger(name)

    if not logger.handlers:
        fh = RotatingFileHandler(
            client_dir / f"{conv_id}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
        logger.propagate = True  # 同时写全局日志

    _conv_loggers[key] = logger
    return logger
