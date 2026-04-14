"""
工具 / 产物 / 沙箱路由 — 工具列表、产物下载、沙箱状态
"""
import base64
import json as _json
import logging
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from tools import get_tools_info
from db.artifact_store import get_artifact_content

logger = logging.getLogger("routers.tool")

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools")
async def list_tools():
    """列出当前所有可用工具（内置 + MCP + 动态注册）。"""
    return {"tools": get_tools_info()}


@router.get("/artifacts/{artifact_id}")
async def get_artifact_detail(artifact_id: int):
    """按需加载单个产物的完整内容（含二进制、slides_html 等）。"""
    data = await get_artifact_content(artifact_id)
    if not data:
        return {"error": "产物不存在"}
    return data


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: int):
    """下载文件产物（二进制流，浏览器直接触发下载）。"""
    data = await get_artifact_content(artifact_id)
    if not data:
        return {"error": "产物不存在"}

    name = data.get("name", "download")
    content = data.get("content", "")
    language = data.get("language", "text")

    # 二进制类型（pptx/archive）：从 binary_b64 解码
    if data.get("binary") or language in ("archive", "pptx", "pdf"):
        binary_b64 = content
        if content.startswith("{"):
            try:
                packed = _json.loads(content)
                binary_b64 = packed.get("binary_b64", content)
            except _json.JSONDecodeError:
                pass
        try:
            raw_bytes = base64.b64decode(binary_b64)
        except Exception:
            return {"error": "文件解码失败"}

        mime_map = {
            "archive": "application/gzip",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pdf": "application/pdf",
        }
        mime = mime_map.get(language, "application/octet-stream")
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
