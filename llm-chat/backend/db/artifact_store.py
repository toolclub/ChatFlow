"""
artifact_store：文件产物的 DB 持久化层

职责：
  - save_artifact:         sandbox_write 成功后保存文件产物
  - get_artifacts_for_conv: 前端刷新后恢复文件产物列表

设计原则：
  - 所有操作均 try/except，失败仅记录日志，不阻断主流程
  - 同一路径重复写入时更新内容（upsert 语义）
"""
import logging
import time

from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import ArtifactModel

logger = logging.getLogger("db.artifact_store")

# 文件后缀 → 语言标记
_EXT_MAP = {
    "html": "html", "htm": "html", "svg": "svg", "css": "css",
    "js": "javascript", "mjs": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "py": "python", "rb": "ruby", "go": "go", "rs": "rust",
    "java": "java", "kt": "kotlin", "c": "c", "cpp": "cpp", "h": "c",
    "sh": "shell", "bash": "shell", "zsh": "shell",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml",
    "xml": "xml", "md": "markdown", "sql": "sql", "vue": "vue",
    "txt": "text", "csv": "text", "log": "text",
}


def detect_language(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_MAP.get(ext, "text")


async def save_artifact(
    conv_id: str,
    name: str,
    path: str,
    content: str,
    language: str | None = None,
) -> dict:
    """保存文件产物，返回产物数据 dict（供 SSE 发送）。"""
    lang = language or detect_language(path)
    now = time.time()
    artifact_data = {
        "name": name,
        "path": path,
        "language": lang,
        "content": content,
        "created_at": now,
    }
    try:
        async with AsyncSessionLocal() as session:
            # Upsert: 同一对话同一路径只保留最新版
            existing = await session.execute(
                select(ArtifactModel).where(
                    ArtifactModel.conv_id == conv_id,
                    ArtifactModel.path == path,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.content = content
                row.language = lang
                row.created_at = now
            else:
                session.add(ArtifactModel(
                    conv_id=conv_id,
                    name=name,
                    path=path,
                    language=lang,
                    content=content,
                    created_at=now,
                ))
            await session.commit()
    except Exception:
        logger.exception("save_artifact failed | conv=%s path=%s", conv_id, path)
    return artifact_data


async def get_artifacts_for_conv(conv_id: str) -> list[dict]:
    """获取对话的所有文件产物（按创建时间排序）。"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ArtifactModel)
                .where(ArtifactModel.conv_id == conv_id)
                .order_by(ArtifactModel.created_at)
            )
            rows = result.scalars().all()
            return [
                {
                    "name": r.name,
                    "path": r.path,
                    "language": r.language,
                    "content": r.content,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("get_artifacts_for_conv failed | conv=%s", conv_id)
        return []
