"""
artifact_store：文件产物的 DB 持久化层

架构：
  - save_artifact:            工具执行时保存完整内容（含二进制）
  - get_artifact_meta_list:   full-state 加载时只返回元数据（不含 content）→ 加载快
  - get_artifact_content:     前端点击时按需加载完整内容（含二进制）→ 按需取
  - get_artifacts_for_conv:   兼容旧接口（完整返回，含内容）

设计原则：
  - artifacts 通过 message_id 关联到具体消息（外键语义）
  - 同一对话同一路径只保留最新版（upsert）
  - PPT 等二进制文件以 JSON 打包存储（binary_b64 + slides_html）
  - 所有操作 try/except，失败仅记录日志
"""
import logging
import time

from sqlalchemy import select, update as sa_update

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
    "pptx": "pptx", "ppt": "pptx", "pdf": "pdf",
    "tar": "archive", "gz": "archive", "zip": "archive", "tgz": "archive",
    "jar": "archive", "war": "archive", "ear": "archive",
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
    message_id: str = "",
    size: int = 0,
    slide_count: int = 0,
    source: str = "generated",
) -> dict:
    """保存文件产物（upsert），返回产物元数据 dict。

    source: 'generated'（沙箱工具产出，默认）| 'uploaded'（用户上传）
    """
    lang = language or detect_language(path)
    now = time.time()
    artifact_data = {
        "name": name, "path": path, "language": lang,
        "message_id": message_id, "size": size,
        "slide_count": slide_count, "source": source, "created_at": now,
    }
    try:
        async with AsyncSessionLocal() as session:
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
                row.message_id = message_id or row.message_id
                row.size = size or row.size
                row.slide_count = slide_count or row.slide_count
                row.source = source or row.source
                row.created_at = now
                await session.commit()
                artifact_data["id"] = row.id
            else:
                new_row = ArtifactModel(
                    conv_id=conv_id, message_id=message_id,
                    name=name, path=path, language=lang,
                    content=content, size=size, slide_count=slide_count,
                    source=source, created_at=now,
                )
                session.add(new_row)
                await session.flush()  # flush 拿到自增 ID
                artifact_data["id"] = new_row.id
                await session.commit()
    except Exception:
        logger.exception("save_artifact failed | conv=%s path=%s", conv_id, path)
    return artifact_data


async def bind_artifacts_to_message(
    artifact_ids: list[int], conv_id: str, message_id: str,
) -> int:
    """把待绑定的上传 artifacts 关联到具体的 user 消息 ID。

    安全约束（防越权）：
      - 只更新 conv_id 匹配的行 → 跨对话 ID 被忽略
      - 只更新 source='uploaded' 行 → 不会污染工具产出
      - 只更新 message_id='' 行 → 已绑定的不重复改

    返回实际更新的行数。失败返回 0（异常已记录日志）。
    """
    if not artifact_ids:
        return 0
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_update(ArtifactModel)
                .where(
                    ArtifactModel.id.in_(artifact_ids),
                    ArtifactModel.conv_id == conv_id,
                    ArtifactModel.source == "uploaded",
                    ArtifactModel.message_id == "",
                )
                .values(message_id=message_id)
            )
            await session.commit()
            return result.rowcount or 0
    except Exception:
        logger.exception(
            "bind_artifacts_to_message failed | conv=%s msg=%s ids=%s",
            conv_id, message_id, artifact_ids,
        )
        return 0


async def get_artifacts_by_ids(
    artifact_ids: list[int], conv_id: str,
) -> list[dict]:
    """按 ID 批量获取 artifacts 元数据（不含 content），约束 conv_id 防越权。

    用于 chat ingress 在注入 HumanMessage 前缀时取文件清单。
    """
    if not artifact_ids:
        return []
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(
                    ArtifactModel.id, ArtifactModel.name, ArtifactModel.path,
                    ArtifactModel.language, ArtifactModel.size,
                    ArtifactModel.source, ArtifactModel.message_id,
                )
                .where(
                    ArtifactModel.id.in_(artifact_ids),
                    ArtifactModel.conv_id == conv_id,
                )
                .order_by(ArtifactModel.id)
            )
            rows = result.all()
        return [
            {
                "id": r.id, "name": r.name, "path": r.path,
                "language": r.language, "size": r.size,
                "source": r.source, "message_id": r.message_id,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("get_artifacts_by_ids failed | conv=%s ids=%s", conv_id, artifact_ids)
        return []


async def get_artifact_meta_list(conv_id: str) -> list[dict]:
    """
    获取对话的产物元数据列表（不含 content）。

    用于 full-state API，加载快。前端点击时再按需取内容。
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(
                    ArtifactModel.id, ArtifactModel.conv_id,
                    ArtifactModel.message_id, ArtifactModel.name,
                    ArtifactModel.path, ArtifactModel.language,
                    ArtifactModel.size, ArtifactModel.slide_count,
                    ArtifactModel.source, ArtifactModel.created_at,
                )
                .where(ArtifactModel.conv_id == conv_id)
                .order_by(ArtifactModel.created_at)
            )
            rows = result.all()
        return [
            {
                "id": r.id,
                "message_id": r.message_id,
                "name": r.name,
                "path": r.path,
                "language": r.language,
                "size": r.size,
                "slide_count": r.slide_count,
                "source": r.source,
                "binary": r.language in ("pptx", "pdf", "archive"),
                "created_at": r.created_at,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("get_artifact_meta_list failed | conv=%s", conv_id)
        return []


async def get_artifact_content(artifact_id: int) -> dict | None:
    """
    按 ID 获取单个产物的完整内容（含二进制）。

    用于前端点击下载/预览时按需加载。
    PPT 自动解包 JSON（binary_b64 + slides_html）。
    用户上传文件（source='uploaded'）一律按二进制处理。
    """
    import json as _json
    try:
        async with AsyncSessionLocal() as session:
            row = await session.get(ArtifactModel, artifact_id)
            if not row:
                return None
        item: dict = {
            "id": row.id,
            "name": row.name,
            "path": row.path,
            "language": row.language,
            "content": row.content,
            "size": row.size,
            "slide_count": row.slide_count,
            "source": row.source,
            "created_at": row.created_at,
        }
        # PPT：解包 JSON（slides_html + theme）
        if row.language == "pptx" and row.content.startswith("{"):
            try:
                packed = _json.loads(row.content)
                item["content"] = packed.get("binary_b64", "")
                item["slides_html"] = packed.get("slides_html", [])
                item["slide_count"] = packed.get("slide_count", row.slide_count)
                item["theme"] = packed.get("theme", "")
                item["binary"] = True
            except _json.JSONDecodeError:
                pass
        # Archive：解包 JSON（只有 binary_b64 + original_size）
        elif row.language == "archive" and row.content.startswith("{"):
            try:
                packed = _json.loads(row.content)
                item["content"] = packed.get("binary_b64", "")
                item["size"] = packed.get("original_size", row.size)
                item["binary"] = True
            except _json.JSONDecodeError:
                pass
        # 用户上传：无论扩展名一律二进制（保留原始字节，下载即原文件）
        elif row.source == "uploaded" and row.content.startswith("{"):
            try:
                packed = _json.loads(row.content)
                item["content"] = packed.get("binary_b64", "")
                item["size"] = packed.get("original_size", row.size)
                item["mime"] = packed.get("mime", "application/octet-stream")
                item["binary"] = True
            except _json.JSONDecodeError:
                pass
        return item
    except Exception:
        logger.exception("get_artifact_content failed | id=%s", artifact_id)
        return None


async def get_artifacts_for_conv(conv_id: str) -> list[dict]:
    """兼容旧接口：返回完整内容（含解包）。新代码应用 get_artifact_meta_list + get_artifact_content。"""
    import json as _json
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ArtifactModel)
                .where(ArtifactModel.conv_id == conv_id)
                .order_by(ArtifactModel.created_at)
            )
            rows = result.scalars().all()
            artifacts = []
            for r in rows:
                item: dict = {
                    "id": r.id, "message_id": getattr(r, "message_id", ""),
                    "name": r.name, "path": r.path, "language": r.language,
                    "content": r.content, "size": getattr(r, "size", 0),
                    "slide_count": getattr(r, "slide_count", 0),
                    "created_at": r.created_at,
                }
                if r.language == "pptx" and r.content.startswith("{"):
                    try:
                        packed = _json.loads(r.content)
                        item["content"] = packed.get("binary_b64", "")
                        item["slides_html"] = packed.get("slides_html", [])
                        item["slide_count"] = packed.get("slide_count", 0)
                        item["theme"] = packed.get("theme", "")
                        item["binary"] = True
                    except _json.JSONDecodeError:
                        pass
                artifacts.append(item)
            return artifacts
    except Exception:
        logger.exception("get_artifacts_for_conv failed | conv=%s", conv_id)
        return []
