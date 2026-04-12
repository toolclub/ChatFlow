"""
PPT 生成工具 — HTML-first 方案

流程：
  1. 模型为每张幻灯片输出完整 HTML（含内联 CSS）
  2. 后端生成自包含 Python 脚本（使用 Playwright 截图 + python-pptx 嵌入）
  3. 在沙箱中执行 → 生成 .pptx
  4. 从沙箱读取 .pptx → 保存为 artifact → 通知前端
  5. HTML 同时作为前端预览
"""
import base64
import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger("tools.ppt")


def _get_conv_id() -> str:
    from sandbox.context import current_conv_id
    return current_conv_id.get() or "default"


def _get_message_id() -> str:
    from sandbox.context import current_message_id
    return current_message_id.get() or ""


@tool
async def create_ppt(ppt_json: str) -> str:
    """
    根据描述创建 PowerPoint 演示文稿。你需要为每张幻灯片编写完整的 HTML 页面。

    ppt_json 必须是一个 JSON 字符串，格式如下：
    {
      "title": "演示文稿标题",
      "slides": [
        {
          "title": "幻灯片标题（用于索引）",
          "html": "<!DOCTYPE html><html><head><style>完整CSS样式</style></head><body>完整HTML内容</body></html>"
        }
      ]
    }

    HTML 编写要求：
    1. 每页 HTML 是一个完整的页面，宽 960px 高 720px（16:9 宽高比）
    2. 必须内联所有 CSS（不能引用外部资源）
    3. 使用 body { width:960px; height:720px; margin:0; overflow:hidden; } 固定尺寸
    4. 字体使用 'Microsoft YaHei','PingFang SC','Helvetica Neue',sans-serif
    5. 设计要精美：合理使用渐变背景、圆角卡片、阴影、图标符号(emoji)、色块装饰等
    6. 标题页要大气，内容页要清晰易读，结束页要简洁
    7. 可以使用 CSS Grid / Flexbox 进行复杂布局
    8. 配色要统一协调，建议选定一个主色调贯穿所有页面

    设计风格参考：
    - 封面页：大标题居中 + 副标题 + 装饰元素（渐变色块、几何图形等）
    - 内容页：左侧标题区 + 右侧要点列表，或上下分区布局
    - 数据页：用 CSS 绘制简单柱形图 / 进度条 / 数据卡片
    - 对比页：左右双栏对比布局
    - 引用页：大号引号装饰 + 引文 + 作者
    - 结束页：居中感谢语 + 联系方式

    重要：每页的 html 字段必须是完整的 HTML 文档（包含 <!DOCTYPE html>）。

    Args:
        ppt_json: PPT 描述的 JSON 字符串

    Returns:
        创建结果
    """
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager
    from db.artifact_store import save_artifact

    conv_id = _get_conv_id()

    # ── 解析 JSON ──
    try:
        if isinstance(ppt_json, dict):
            ppt_data = ppt_json
        elif isinstance(ppt_json, str):
            cleaned = ppt_json.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
            try:
                ppt_data = json.loads(cleaned)
            except json.JSONDecodeError:
                import re
                cleaned = re.sub(r'[\x00-\x1f](?<![\\][\x00-\x1f])', ' ', cleaned)
                ppt_data = json.loads(cleaned)
        else:
            return f"参数类型错误: {type(ppt_json)}"
    except json.JSONDecodeError as exc:
        return f"JSON 解析失败: {exc}\n请确保 JSON 格式正确。"

    ppt_title = ppt_data.get("title", "presentation")
    slides = ppt_data.get("slides", [])
    if not slides:
        return "错误: slides 数组为空"

    # 提取 HTML 列表
    slides_html = []
    for s in slides:
        html = s.get("html", "")
        if not html:
            # 兼容旧格式：如果没有 html 字段，用 fallback 生成
            html = _fallback_slide_html(s)
        slides_html.append(html)

    filename = f"{ppt_title.replace(' ', '_')[:30]}.pptx"

    logger.info("create_ppt | conv=%s | title=%s | slides=%d", conv_id, ppt_title, len(slides))

    async def _notify(text: str) -> None:
        """安全地发送 sandbox_output 事件（失败不阻断主流程）。"""
        try:
            await adispatch_custom_event("sandbox_output", {
                "stream": "stdout",
                "text": text,
                "tool_name": "create_ppt",
            })
        except Exception as e:
            logger.warning("sandbox_output 事件发送失败: %s", e)

    # ── 诊断日志：确认 _notify 是否被调用、是否成功 ──
    logger.warning("[PPT诊断] create_ppt 进入执行，准备 _notify | conv=%s slides=%d", conv_id, len(slides))
    await _notify(f"📊 正在生成 PPT: {ppt_title} ({len(slides)} 页)\n")
    logger.warning("[PPT诊断] _notify 完成（无异常）")

    # ── 文生图：为有 image_prompt 的幻灯片生成配图 ──
    from config import IMAGE_GEN_ENABLED
    for i, slide in enumerate(slides):
        img_prompt = slide.get("image_prompt")
        if not img_prompt or not IMAGE_GEN_ENABLED:
            continue
        await _notify(f"🎨 第 {i+1} 页：正在调用文生图 API...\n")
        try:
            from ppt.image_gen import generate_image
            img_bytes = await generate_image(img_prompt)
            if img_bytes:
                img_filename = f"slide_{i+1}_img.jpg"
                await _upload_binary_to_sandbox(sandbox_manager, conv_id, img_filename, img_bytes)
                b64_img = base64.b64encode(img_bytes).decode("ascii")
                slides_html[i] = slides_html[i].replace(
                    f"[IMG_PLACEHOLDER_{i+1}]",
                    f"data:image/jpeg;base64,{b64_img}"
                )
                await _notify(f"  ✅ 配图已生成 ({len(img_bytes)//1024}KB)\n")
        except Exception as exc:
            logger.warning("配图失败: %s", exc)
            await _notify(f"  ⚠️ 配图失败: {exc}\n")

    # ── 生成渲染脚本 ──
    await _notify("📝 正在渲染幻灯片（HTML → 截图 → PPT）...\n")

    from ppt.script_gen import generate_html_render_script
    script = generate_html_render_script(slides_html, filename)

    # ── 在沙箱中执行（on_output 回调也做安全包装） ──
    async def _safe_output(stream: str, text: str):
        try:
            await adispatch_custom_event("sandbox_output", {
                "stream": stream, "text": text, "tool_name": "create_ppt",
            })
        except Exception:
            pass  # 沙箱输出事件失败不影响 PPT 生成

    result = await sandbox_manager.execute_code_streaming(
        conv_id, "python", script,
        on_output=_safe_output,
    )

    # 保留脚本终端输出，供 SandboxFormatter 推送给前端渲染终端 UI
    script_display = result.to_display()

    if result.exit_code != 0:
        return f"{script_display}\n\nPPT 渲染失败:\n{result.stderr or result.stdout}"

    # ── 从沙箱读取 .pptx 并保存为 artifact ──
    try:
        worker, session_dir = await sandbox_manager._get_worker_for_session(conv_id)
        read_result = await worker.exec_command(
            f"base64 -w0 {session_dir}/{filename}", timeout=30,
        )
        if read_result.exit_code == 0 and read_result.stdout.strip():
            content_b64 = read_result.stdout.strip()
            file_bytes = base64.b64decode(content_b64)
            size_kb = len(file_bytes) / 1024

            # ── 存 DB：binary + slides_html + 元数据（刷新恢复用）──
            import json as _json
            artifact_content = _json.dumps({
                "binary_b64": content_b64,
                "slides_html": slides_html,
                "slide_count": len(slides),
                "theme": "html",
            }, ensure_ascii=False)

            msg_id = _get_message_id()
            save_ok = await save_artifact(
                conv_id, filename, filename, artifact_content,
                message_id=msg_id, size=len(file_bytes), slide_count=len(slides),
            )
            logger.info(
                "artifact 保存 | conv=%s | msg=%s | file=%s | size=%dKB",
                conv_id, msg_id, filename, size_kb,
            )

            # ── SSE 推送：包含 slides_html 供前端实时预览 ──
            await adispatch_custom_event("file_artifact", {
                "name": filename,
                "path": filename,
                "content": content_b64,
                "language": "pptx",
                "binary": True,
                "size": len(file_bytes),
                "slide_count": len(slides),
                "theme": "html",
                "slides_html": slides_html,
            })

            await _notify(f"\n✅ PPT 已生成: {filename} ({size_kb:.1f}KB, {len(slides)} 页)\n")

            return (
                f"{script_display}\n\n"
                f"PPT 已成功创建！\n"
                f"文件名: {filename}\n"
                f"页数: {len(slides)}\n"
                f"大小: {size_kb:.1f}KB\n"
                f"用户可点击文件卡片下载。"
            )
        else:
            logger.error("PPT 文件读取失败 | conv=%s | stderr=%s", conv_id, read_result.stderr)
            return f"文件读取失败: {read_result.stderr}"
    except Exception as exc:
        logger.exception("artifact 保存异常 | conv=%s", conv_id)
        return f"artifact 保存失败: {exc}"


def _fallback_slide_html(slide: dict) -> str:
    """兼容旧格式（无 html 字段时），从结构化字段生成简单 HTML。"""
    layout = slide.get("layout", "content")
    title = slide.get("title", "")
    subtitle = slide.get("subtitle", "")
    bullets = slide.get("bullets", [])
    text = slide.get("text", "")

    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
    body = ""
    if layout == "title":
        body = f'<h1 style="font-size:42px;color:#1a73e8;margin-bottom:16px;">{title}</h1>'
        if subtitle:
            body += f'<p style="font-size:20px;color:#666;">{subtitle}</p>'
    elif layout == "ending":
        body = f"""
        <div style="text-align:center;">
            <div style="width:80px;height:4px;background:#1a73e8;margin:0 auto 20px;border-radius:2px;"></div>
            <h1 style="font-size:44px;color:#1a73e8;">{title}</h1>
            <p style="font-size:18px;color:#666;margin-top:12px;">{subtitle}</p>
        </div>"""
    else:
        body = f'<h2 style="font-size:28px;color:#1a73e8;margin-bottom:8px;">{title}</h2>'
        body += '<div style="width:60px;height:3px;background:#4285f4;border-radius:2px;margin-bottom:20px;"></div>'
        if bullets:
            body += f'<ul style="list-style:none;padding:0;">{bullets_html}</ul>'
        if text:
            body += f'<p style="font-size:16px;line-height:1.8;">{text}</p>'

    return f"""<!DOCTYPE html><html><head><style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{width:960px;height:720px;overflow:hidden;background:#fff;color:#333;
font-family:'Microsoft YaHei','PingFang SC','Helvetica Neue',sans-serif;
display:flex;flex-direction:column;justify-content:center;padding:60px 80px;}}
ul li{{padding:8px 0 8px 24px;font-size:17px;position:relative;line-height:1.6;}}
ul li::before{{content:'';position:absolute;left:0;top:16px;width:8px;height:8px;border-radius:50%;background:#4285f4;}}
</style></head><body>{body}</body></html>"""


async def _upload_binary_to_sandbox(manager, conv_id: str, filename: str, data: bytes) -> None:
    """将二进制数据上传到沙箱会话目录。"""
    worker, session_dir = await manager._get_worker_for_session(conv_id)
    remote_path = f"{session_dir}/{filename}"
    b64 = base64.b64encode(data).decode("ascii")
    chunk_size = 50000
    await worker.exec_command(f"> {remote_path}.b64")
    for i in range(0, len(b64), chunk_size):
        chunk = b64[i:i+chunk_size]
        await worker.exec_command(f"echo -n '{chunk}' >> {remote_path}.b64")
    await worker.exec_command(f"base64 -d {remote_path}.b64 > {remote_path} && rm {remote_path}.b64")
