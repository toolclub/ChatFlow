"""
用户文件上传路由 — 把前端拖入/选择的非图片文件落到沙箱 + DB

设计原则（spec.md 兼容）：
  - 状态全部走 DB（artifacts 表 + sandbox session_dir 远端 FS），无进程内 dict
  - sandbox worker 亲和性由 SandboxManager._get_worker_for_session 处理（DB 持久化）
  - artifact 通过 source='uploaded' + message_id='' 标记"待绑定"，发送消息时 UPDATE
  - 文件二进制双写：① 写沙箱（模型立刻能 run_shell 操作） ② 存 DB（durable，session 过期也能下载）

为什么 DB 也存内容（base64 in JSONB-style packed）：
  - 沙箱 session_dir 12h 不用就被清理，但用户期望刷新历史还能下载原文件
  - artifacts 表已是文件产物的 source of truth，扩展 source 列即可，无需新表
  - 50MB 文件 base64 → 67MB TEXT，PostgreSQL 1GB 上限内绰绰有余
"""
import base64
import json
import logging
import mimetypes

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from config import UPLOAD_MAX_FILE_SIZE
from db.artifact_store import detect_language, save_artifact
from db.database import AsyncSessionLocal
from db.models import ConversationModel
from sandbox.manager import sandbox_manager

logger = logging.getLogger("routers.files")

router = APIRouter(prefix="/api", tags=["files"])


@router.post("/files/upload")
async def upload_file(
    conv_id: str = Form(...),
    file: UploadFile = File(...),
):
    """上传文件到沙箱 + 持久化到 artifacts。

    返回 {id, name, path, size, language, source} —— 前端拿 id 在发消息时带 file_ids。
    """
    # ── 1. 校验对话存在（防止往野 conv 上传文件） ──
    async with AsyncSessionLocal() as session:
        conv = await session.get(ConversationModel, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail=f"对话不存在: {conv_id}")

    # ── 2. 校验沙箱可用 ──
    if not sandbox_manager.available:
        raise HTTPException(status_code=503, detail="沙箱不可用：无健康 Worker")

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # ── 3. 读字节 + 大小校验（流式读，避免 50MB 全压栈） ──
    # FastAPI UploadFile 内部用 SpooledTemporaryFile，超过阈值落盘，安全
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)  # 1MB 一块
        if not chunk:
            break
        total += len(chunk)
        if total > UPLOAD_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大（>{UPLOAD_MAX_FILE_SIZE // 1024 // 1024}MB）",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    # ── 4. 写入沙箱（SFTP）── 内部会做 basename + 同名去重 ──
    try:
        place = await sandbox_manager.upload_binary(
            conv_id, filename, data, max_size=UPLOAD_MAX_FILE_SIZE,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("沙箱写入失败 | conv=%s filename=%s", conv_id, filename)
        raise HTTPException(status_code=500, detail=f"沙箱写入失败: {exc}")

    # ── 5. 持久化到 artifacts（content 是 base64 packed JSON，与 archive/pptx 同格式） ──
    mime, _ = mimetypes.guess_type(place["filename"])
    mime = mime or (file.content_type or "application/octet-stream")
    packed = json.dumps(
        {
            "binary_b64": base64.b64encode(data).decode("ascii"),
            "original_size": total,
            "mime": mime,
        },
        ensure_ascii=False,
    )
    artifact = await save_artifact(
        conv_id=conv_id,
        name=place["filename"],
        path=place["abs_path"],     # 全路径，模型用 sandbox_read 时直接定位
        content=packed,
        language=detect_language(place["filename"]),
        message_id="",              # 待 chat 发送时 bind_artifacts_to_message 填
        size=total,
        source="uploaded",
    )

    if not artifact.get("id"):
        # save_artifact 内部异常已被 logger.exception；这里再兜底
        raise HTTPException(status_code=500, detail="artifact 持久化失败（查 backend 日志）")

    logger.info(
        "上传完成 | conv=%s | id=%d | name=%s | size=%d | path=%s",
        conv_id, artifact["id"], place["filename"], total, place["abs_path"],
    )

    return {
        "id": artifact["id"],
        "name": place["filename"],
        "path": place["abs_path"],
        "rel_path": place["rel_path"],
        "size": total,
        "language": artifact["language"],
        "mime": mime,
        "source": "uploaded",
    }
