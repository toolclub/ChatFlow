"""
工具 / 产物 / 沙箱路由 — 工具列表、产物下载、沙箱状态
"""
import base64
import json as _json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from tools import get_tools_info
from db.artifact_store import get_artifact_content
from db.database import AsyncSessionLocal
from db.models import ConversationModel
from services.auth.dependencies import CurrentUser

logger = logging.getLogger("routers.tool")

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools")
async def list_tools():
    """列出当前所有可用工具（内置 + MCP + 动态注册）。"""
    return {"tools": get_tools_info()}


async def _check_artifact_access(artifact_data: dict, user: dict):
    """检查用户是否有权访问该产物"""
    conv_id = artifact_data.get("conv_id")
    if not conv_id:
        raise HTTPException(status_code=404, detail="产物未关联对话")
    
    async with AsyncSessionLocal() as session:
        conv = await session.get(ConversationModel, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    
    if user.get("id"):
        if conv.user_id != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该产物")
    else:
        if conv.user_id or conv.client_id != user.get("client_id"):
            raise HTTPException(status_code=403, detail="无权访问该产物")


@router.get("/artifacts/{artifact_id}")
async def get_artifact_detail(artifact_id: int, user: CurrentUser):
    """按需加载单个产物的完整内容（含二进制、slides_html 等）。"""
    data = await get_artifact_content(artifact_id)
    if not data:
        raise HTTPException(status_code=404, detail="产物不存在")
    
    await _check_artifact_access(data, user)
    return data


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: int, user: CurrentUser):
    """下载文件产物（二进制流，浏览器直接触发下载）。"""
    data = await get_artifact_content(artifact_id)
    if not data:
        raise HTTPException(status_code=404, detail="产物不存在")

    await _check_artifact_access(data, user)

    name = data.get("name", "download")
    content = data.get("content", "")
    language = data.get("language", "text")
    is_uploaded = data.get("source") == "uploaded"

    # 二进制类型（pptx/archive/pdf/用户上传）：从 binary_b64 解码
    if data.get("binary") or language in ("archive", "pptx", "pdf") or is_uploaded:
        binary_b64 = content
        if content.startswith("{"):
            try:
                packed = _json.loads(content)
                binary_b64 = packed.get("binary_b64", content)
            except _json.JSONDecodeError:
                pass
        try:
            raw_bytes = base64.b64decode(binary_b64, validate=True)
        except (ValueError, TypeError):
            logger.error("产物二进制内容无效 | artifact=%s", artifact_id)
            raise HTTPException(status_code=422, detail="文件内容损坏，无法下载")

        # 用户上传：优先用上传时记录的 mime；否则按 language + 扩展名映射
        mime_map = {
            "archive": "application/gzip",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pdf": "application/pdf",
        }
        # archive 下细分：jar/war/ear 有独立 mime，避免下载工具误判扩展名
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        archive_mime_by_ext = {
            "jar": "application/java-archive",
            "war": "application/java-archive",
            "ear": "application/java-archive",
            "zip": "application/zip",
        }
        mime = (
            data.get("mime")
            or archive_mime_by_ext.get(ext)
            or mime_map.get(language, "application/octet-stream")
        )
        return Response(
            content=raw_bytes,
            media_type=mime,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )

    # 文本类型
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/sandbox/status")
async def sandbox_status():
    """查看沙箱 Worker 集群状态。"""
    from config import SANDBOX_ENABLED
    if not SANDBOX_ENABLED:
        return {"enabled": False}
    from sandbox.manager import sandbox_manager
    return {"enabled": True, **sandbox_manager.status()}
