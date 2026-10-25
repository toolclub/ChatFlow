"""
沙箱代码执行工具（流式输出 + 智能后台进程版）

核心改进：
  1. 代码执行过程中，stdout/stderr 实时通过 adispatch_custom_event 推送给前端
  2. 长驻服务（http.server、flask、node server 等）自动后台启动，不阻塞
  3. 服务启动后自动验证（检查进程、端口、日志）

命令分类策略：
  - 服务类命令：自动 nohup 后台化 + 端口验证 + 日志查看
  - 普通命令：正常流式执行 + 输出重定向 + 退出码检查
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = (
    "沙箱是你的工作台——有独立的 session 目录、可写文件、可执行代码、可安装依赖。"
    "工具集分两层：\n"
    "① 写完代码必须验证——sandbox_write 后紧接 execute_code 或 run_shell；\n"
    "② 禁止在前台启动任何长驻服务（uvicorn / flask / node / java 等）——必须 nohup 后台化，否则 120s 超时卡死。\n"
    "缺依赖先用 run_shell pip install。HTML/SVG/CSS 文件写入后前端自动预览，无需额外启动。"
)
ERROR_HINT = (
    "代码执行失败请检查语法和依赖，可 run_shell 查看错误日志后修复重试。"
    "超时时改用后台执行：nohup <命令> > /tmp/out.log 2>&1 & 然后 tail -50 /tmp/out.log 查看进度。"
)
TAGS = ["sandbox", "code"]
# 渲染模式：terminal=命令终端 file_write=文件写入 file_read=文件读取
# 单模块多工具时用 DISPLAY_MODES dict，key 是工具函数名
DISPLAY_MODES = {
    "execute_code": "terminal",
    "run_shell": "terminal",
    "sandbox_write": "file_write",
    "sandbox_read": "file_read",
    "sandbox_download": "terminal",
}
import json
import logging
import re

from langchain_core.tools import tool

logger = logging.getLogger("tools.sandbox")

# ── 服务类命令检测模式 ──────────────────────────────────────────────────────
_SERVICE_PATTERNS = [
    # ── Python ──
    r"python[23]?\s+(-m\s+)?http\.server",
    r"python[23]?\s+(-m\s+)?flask\s+run",
    r"python[23]?\s+(-m\s+)?uvicorn",
    r"python[23]?\s+(-m\s+)?gunicorn",
    r"python[23]?\s+(-m\s+)?streamlit\s+run",
    r"python[23]?\s+(-m\s+)?gradio",
    r"python[23]?\s+(-m\s+)?tornado",
    r"python[23]?\s+(-m\s+)?django\s+runserver",
    r"python[23]?\s+.*\.run\(",                    # app.run() / server.run()
    r"uvicorn\s+",                                  # 直接调 uvicorn（无 python 前缀）
    r"gunicorn\s+",
    r"celery\s+worker",
    r"daphne\s+",
    # ── Node.js / Deno / Bun ──
    r"node\s+\S+\.\w+",                              # node app.js / node index.js（排除 node -v 等）
    r"npm\s+(start|run\s+\w+)",                     # npm start / npm run dev|serve|build|watch
    r"npx\s+(serve|next|nuxt|vite|ts-node|tsx)",
    r"yarn\s+(start|dev|serve)",
    r"pnpm\s+(start|dev|serve)",
    r"deno\s+run",
    r"bun\s+run",
    r"ts-node\s+",
    r"tsx\s+",
    r"next\s+(dev|start)",
    r"nuxt\s+(dev|start)",
    r"vite\s+(dev|preview)",
    # ── Java / JVM ──
    r"java\s+.*-jar",
    r"java\s+.*\.(Main|Application|Server|Boot)",
    r"mvn\s+spring-boot:run",
    r"gradle\s+bootRun",
    r"gradlew\s+bootRun",
    r"\./gradlew\s+bootRun",
    r"mvnw\s+spring-boot:run",
    r"\./mvnw\s+spring-boot:run",
    r"java\s+.*-cp\s+",                             # java -cp xxx MainClass
    r"kotlin\s+.*server",
    # ── Go ──
    r"go\s+run\s+",
    r"\./\w+.*server",                               # ./myserver
    # ── Rust ──
    r"cargo\s+run",
    # ── Ruby ──
    r"ruby\s+.*server",
    r"rails\s+server",
    r"rails\s+s\b",
    r"rackup",
    r"puma\s+",
    r"thin\s+start",
    # ── PHP ──
    r"php\s+-S",
    r"php\s+artisan\s+serve",
    r"php\s+.*-S\s+",
    # ── .NET ──
    r"dotnet\s+run",
    r"dotnet\s+watch",
    # ── 通用服务 / 中间件 ──
    r"nginx",
    r"httpd",
    r"apache2",
    r"redis-server",
    r"mongod",
    r"mysqld",
    r"postgres",
    r"docker\s+run\s+.*-p\s+",                      # docker run -p 映射端口
    r"docker-compose\s+up",
    r"docker\s+compose\s+up",
    # ── 已经后台化的（不重复处理） ──
    r"nohup\s+",
]
_SERVICE_RE = re.compile("|".join(f"({p})" for p in _SERVICE_PATTERNS), re.IGNORECASE)

# ── 安装/构建类命令：会结束但耗时长，需要放宽超时 ──────────────────────────────
_INSTALL_PATTERNS = [
    r"\bpip[23]?\s+(install|download|wheel)\b",
    r"\bpoetry\s+(install|add|update|lock)\b",
    r"\buv\s+(pip\s+install|sync|add)\b",
    r"\bconda\s+(install|update|create)\b",
    r"\bnpm\s+(install|ci|i)\b",
    r"\byarn\s+(install|add|upgrade)\b",
    r"\bpnpm\s+(install|add|i)\b",
    r"\bapt(-get)?\s+(install|update|upgrade)\b",
    r"\bapk\s+add\b",
    r"\bbrew\s+(install|upgrade)\b",
    r"\bgem\s+install\b",
    r"\bcargo\s+(install|build|update)\b",
    r"\bgo\s+(install|get|mod\s+download|build)\b",
    r"\bmvn\s+(install|package|compile)\b",
    r"\bgradle\s+(build|assemble)\b",
    r"\b\./gradlew\s+(build|assemble)\b",
    r"\bdotnet\s+(restore|build|publish)\b",
    r"\bcomposer\s+(install|require|update)\b",
]
_INSTALL_RE = re.compile("|".join(f"({p})" for p in _INSTALL_PATTERNS), re.IGNORECASE)
_INSTALL_TIMEOUT = 300  # 5 分钟，够装常见全家桶

# ── 端口提取模式 ────────────────────────────────────────────────────────────
_PORT_PATTERNS = [
    r"(?:--port|:|-p|--bind\s+\S+:)\s*(\d{2,5})",
    r"http\.server\s+(\d{2,5})",
    r"localhost:(\d{2,5})",
    r"0\.0\.0\.0:(\d{2,5})",
    r"127\.0\.0\.1:(\d{2,5})",
    r"port\s*[=:]\s*(\d{2,5})",
    r"server\.port\s*[=:]\s*(\d{2,5})",            # Spring Boot application.properties
    r"PORT\s*[=:]\s*(\d{2,5})",                     # env PORT=3000
    r"-Dserver\.port=(\d{2,5})",                     # Java -D 参数
    r"--listen\s+\S*:(\d{2,5})",                     # 通用 --listen
    r"EXPOSE\s+(\d{2,5})",                           # Dockerfile
    r"runserver\s+(?:\S+:)?(\d{2,5})",              # django runserver 8000 / 0.0.0.0:8000
]
_PORT_RE = re.compile("|".join(_PORT_PATTERNS), re.IGNORECASE)


def _is_service_command(cmd: str) -> bool:
    """检测命令是否为长驻服务类命令。"""
    return bool(_SERVICE_RE.search(cmd))


def _is_install_command(cmd: str) -> bool:
    """检测命令是否为包安装/构建类（允许超时放宽到 _INSTALL_TIMEOUT）。"""
    return bool(_INSTALL_RE.search(cmd))


def _extract_port(cmd: str) -> int | None:
    """从命令中提取端口号。"""
    m = _PORT_RE.search(cmd)
    if m:
        for g in m.groups():
            if g and g.isdigit():
                port = int(g)
                if 1 <= port <= 65535:
                    return port
    return None


def _get_conv_id() -> str:
    from sandbox.context import current_conv_id
    return current_conv_id.get() or "default"


@tool
async def execute_code(language: str, code: str) -> str:
    """
    在沙箱中执行代码——这里有独立的 session 目录、文件系统、可安装依赖。

    写完代码必须执行验证，缺依赖先 pip install。
    禁止在前台启动任何长驻服务（uvicorn / flask / node / java / go / rails 等），
    必须 nohup 后台化，否则 120s 超时卡死。
    同理，任何可能超过 10s 的命令都应后台化：command > /tmp/out.log 2>&1 & 然后 tail 查看日志。

    Args:
        language: 编程语言，可选值: python, javascript, java, shell
        code: 要执行的代码内容

    Returns:
        执行结果，包含完整终端回显
    """
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager

    conv_id = _get_conv_id()
    logger.info("execute_code | conv=%s | lang=%s | code_len=%d", conv_id, language, len(code))

    # shell 脚本中包含服务命令时，转给 run_shell 的服务模式处理（防止超时阻塞）
    is_shell = language.lower().strip() in ("shell", "bash", "sh")
    if is_shell and _is_service_command(code):
        logger.info("execute_code → 检测到服务命令，转入后台模式 | conv=%s", conv_id)
        return await _run_service_command(conv_id, code, sandbox_manager, adispatch_custom_event)

    # 安装/构建类命令放宽超时
    timeout = _INSTALL_TIMEOUT if is_shell and _is_install_command(code) else None
    if timeout:
        logger.info("execute_code → 检测到安装命令，超时放宽到 %ds | conv=%s", timeout, conv_id)

    result = await sandbox_manager.execute_code_streaming(
        conv_id, language, code,
        on_output=lambda stream, text: adispatch_custom_event(
            "sandbox_output",
            {"stream": stream, "text": text, "tool_name": "execute_code"},
        ),
        timeout=timeout,
    )
    return result.to_display()


@tool
async def run_shell(command: str) -> str:
    """
    在沙箱 session 目录里跑 shell 命令——安装依赖、查看日志、检查进程、跑构建命令。
    系统自动检测长驻服务命令并后台化；120s 超时，超时的改用 nohup 后台执行。

    Args:
        command: 要执行的 shell 命令

    Returns:
        命令输出结果
    """
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager

    conv_id = _get_conv_id()
    logger.info("run_shell | conv=%s | cmd='%.100s'", conv_id, command)

    # ── 检测是否为服务类命令 ──
    if _is_service_command(command):
        return await _run_service_command(conv_id, command, sandbox_manager, adispatch_custom_event)

    # ── 安装/构建类命令：放宽超时（pip install、npm install、mvn package 等）──
    timeout = _INSTALL_TIMEOUT if _is_install_command(command) else None
    if timeout:
        logger.info("run_shell → 检测到安装命令，超时放宽到 %ds | conv=%s", timeout, conv_id)

    # ── 普通命令：正常流式执行 ──
    result = await sandbox_manager.run_shell_streaming(
        conv_id, command,
        on_output=lambda stream, text: adispatch_custom_event(
            "sandbox_output",
            {"stream": stream, "text": text, "tool_name": "run_shell"},
        ),
        timeout=timeout,
    )
    return result.to_display()


async def _run_service_command(conv_id, command, manager, dispatch_event):
    """
    服务类命令：后台启动 + 自动验证。

    流程：
      1. 使用 nohup 后台启动，日志输出到 /tmp/service_<port>.log
      2. 等待 2 秒让服务初始化
      3. 验证：检查进程是否存活
      4. 验证：检查端口是否监听
      5. 可选：curl 功能验证
      6. 查看启动日志
    """
    import asyncio

    port = _extract_port(command)
    log_file = f"/tmp/service_{port or 'default'}.log"

    # 如果命令已经包含 nohup 或 &，直接执行
    if "nohup " in command or command.strip().endswith("&"):
        bg_cmd = command
    else:
        bg_cmd = f"nohup {command} > {log_file} 2>&1 &"

    # ── Step 1: 后台启动 ──
    await dispatch_event("sandbox_output", {
        "stream": "stdout",
        "text": f"🚀 正在后台启动服务...\n$ {bg_cmd}\n",
        "tool_name": "run_shell",
    })

    result = await manager.run_shell(conv_id, bg_cmd, timeout=5)

    # 等待服务初始化
    await asyncio.sleep(2)

    output_parts = [f"$ {bg_cmd}"]
    if result.stdout.strip():
        output_parts.append(result.stdout.strip())

    # ── Step 2: 检查进程 ──
    await dispatch_event("sandbox_output", {
        "stream": "stdout",
        "text": "🔍 正在检查进程...\n",
        "tool_name": "run_shell",
    })

    # 从命令中提取关键词用于 grep
    cmd_keyword = command.split()[0] if command.split() else "python"
    if "http.server" in command:
        cmd_keyword = "http.server"
    elif "flask" in command:
        cmd_keyword = "flask"
    elif "uvicorn" in command:
        cmd_keyword = "uvicorn"

    ps_result = await manager.run_shell(
        conv_id, f'ps aux | grep "{cmd_keyword}" | grep -v grep', timeout=5,
    )

    process_alive = ps_result.exit_code == 0 and ps_result.stdout.strip()
    if process_alive:
        output_parts.append(f"\n✅ 进程已启动:\n{ps_result.stdout.strip()}")
        await dispatch_event("sandbox_output", {
            "stream": "stdout",
            "text": f"✅ 进程已启动\n{ps_result.stdout.strip()}\n",
            "tool_name": "run_shell",
        })
    else:
        output_parts.append("\n❌ 进程未找到，服务可能启动失败")
        await dispatch_event("sandbox_output", {
            "stream": "stderr",
            "text": "❌ 进程未找到，服务可能启动失败\n",
            "tool_name": "run_shell",
        })

    # ── Step 3: 检查端口（如果能提取到端口号） ──
    if port:
        await dispatch_event("sandbox_output", {
            "stream": "stdout",
            "text": f"🔍 正在验证端口 {port}...\n",
            "tool_name": "run_shell",
        })

        port_result = await manager.run_shell(
            conv_id, f"ss -tlnp 2>/dev/null | grep :{port} || netstat -tlnp 2>/dev/null | grep :{port}", timeout=5,
        )

        port_listening = port_result.exit_code == 0 and port_result.stdout.strip()
        if port_listening:
            output_parts.append(f"\n✅ 端口 {port} 已监听:\n{port_result.stdout.strip()}")
            await dispatch_event("sandbox_output", {
                "stream": "stdout",
                "text": f"✅ 端口 {port} 已监听\n",
                "tool_name": "run_shell",
            })

            # ── Step 4: 可选功能验证 ──
            curl_result = await manager.run_shell(
                conv_id, f"curl -s --max-time 3 http://localhost:{port}/ 2>/dev/null | head -5", timeout=8,
            )
            if curl_result.exit_code == 0 and curl_result.stdout.strip():
                preview = curl_result.stdout.strip()[:200]
                output_parts.append(f"\n✅ HTTP 响应正常:\n{preview}")
                await dispatch_event("sandbox_output", {
                    "stream": "stdout",
                    "text": f"✅ HTTP 响应正常 (前 200 字符)\n{preview}\n",
                    "tool_name": "run_shell",
                })
        else:
            output_parts.append(f"\n⚠️ 端口 {port} 未监听（服务可能仍在初始化）")
            await dispatch_event("sandbox_output", {
                "stream": "stderr",
                "text": f"⚠️ 端口 {port} 未监听\n",
                "tool_name": "run_shell",
            })

    # ── Step 5: 查看启动日志 ──
    log_result = await manager.run_shell(
        conv_id, f"tail -20 {log_file} 2>/dev/null", timeout=5,
    )
    if log_result.stdout.strip():
        output_parts.append(f"\n📋 启动日志 ({log_file}):\n{log_result.stdout.strip()}")
        await dispatch_event("sandbox_output", {
            "stream": "stdout",
            "text": f"\n📋 启动日志:\n{log_result.stdout.strip()}\n",
            "tool_name": "run_shell",
        })

    # ── 构建最终结果 ──
    final_status = "✅ 服务启动成功" if process_alive else "❌ 服务启动失败"
    if port:
        final_status += f" | 端口: {port}"
    output_parts.insert(0, final_status)

    return "\n".join(output_parts)


@tool
async def sandbox_write(path: str, content: str) -> str:
    """
    把内容写入沙箱 session 目录——这一步产出一个文件，它是后续步骤的输入。
    写入后自动推送 artifact 事件，前端可直接预览（HTML/CSS/SVG）或显示文件卡片。
    写完非平凡代码紧接着用 execute_code / run_shell 验证，不要只写不验。

    Args:
        path: 文件路径（相对于 session 目录）
        content: 文件内容

    Returns:
        写入结果
    """
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager
    from db.artifact_store import save_artifact, detect_language

    conv_id = _get_conv_id()
    from sandbox.context import current_message_id
    msg_id = current_message_id.get() or ""
    logger.info("sandbox_write | conv=%s | path=%s | len=%d", conv_id, path, len(content))
    result = await sandbox_manager.write_file(conv_id, path, content)

    # 写入完成后，保存为文件产物并通知前端
    try:
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        artifact = await save_artifact(conv_id, name, path, content, message_id=msg_id, size=len(content.encode("utf-8")))
        await adispatch_custom_event(
            "file_artifact",
            {**artifact, "content": content, "language": detect_language(path)},
        )
    except Exception:
        logger.warning("file_artifact 事件发送失败 | conv=%s path=%s", conv_id, path, exc_info=True)

    return result.to_display()


@tool
async def sandbox_read(path: str) -> str:
    """
    读取沙箱 session 目录中的文件——在你需要引用或复用已有文件内容时召唤。
    路径相对于当前 session 目录。

    Args:
        path: 文件路径（相对于 session 目录）

    Returns:
        文件内容
    """
    from sandbox.manager import sandbox_manager
    conv_id = _get_conv_id()
    logger.info("sandbox_read | conv=%s | path=%s", conv_id, path)
    result = await sandbox_manager.read_file(conv_id, path)
    return result.to_display()


# 打包文件大小上限（50MB base64 ≈ 37MB 原始）
_MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024


@tool
async def sandbox_download(path: str = ".") -> str:
    """
    把沙箱里的文件或目录打包交给用户——这一步是交付，不是探索。
    path 为目录或 "." 时自动 tar.gz 打包整个 session 目录；为单文件时直接提供下载。
    调用后告诉用户「文件已可下载」和文件名即可，不要在聊天里粘贴内容。

    Args:
        path: 文件或目录路径（相对于 session 目录），默认 "." 表示整个工作目录

    Returns:
        下载结果说明
    """
    import base64
    import shlex
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager
    from sandbox.context import current_message_id
    from db.artifact_store import save_artifact

    conv_id = _get_conv_id()
    msg_id = current_message_id.get() or ""
    logger.info("sandbox_download | conv=%s | path=%s", conv_id, path)

    # 安全：路径中不允许 .. 或绝对路径（防止逃逸）
    sanitized = path.replace("..", "").lstrip("/")
    if not sanitized or sanitized == ".":
        sanitized = "."

    worker, session_dir = await sandbox_manager._get_worker_for_session(conv_id)
    target = f"{session_dir}/{sanitized}" if sanitized != "." else session_dir
    q_target = shlex.quote(target)  # shell 转义，防命令注入

    # 检查路径是文件还是目录
    check = await worker.exec_command(f"test -d {q_target} && echo DIR || (test -f {q_target} && echo FILE || echo NONE)")
    path_type = check.stdout.strip()

    if path_type == "NONE":
        return f"路径不存在: {path}"

    if path_type == "DIR":
        # 打包目录
        archive_name = f"download_{conv_id[:8]}.tar.gz"
        archive_path = f"/tmp/{archive_name}"
        pack_result = await worker.exec_command(
            f"tar -czf {shlex.quote(archive_path)} -C {q_target} . 2>&1 && stat -c%s {shlex.quote(archive_path)}",
            timeout=60,
        )
        if pack_result.exit_code != 0:
            return f"打包失败: {pack_result.stderr or pack_result.stdout}"

        # 读取大小
        size_str = pack_result.stdout.strip().split('\n')[-1]
        try:
            file_size = int(size_str)
        except ValueError:
            file_size = 0

        if file_size > _MAX_DOWNLOAD_SIZE:
            return f"打包文件过大（{file_size // 1024 // 1024}MB），超过 50MB 限制。请指定子目录或具体文件。"

        # base64 读回
        b64_result = await worker.exec_command(f"base64 -w0 {archive_path}", timeout=60)
        content_b64 = b64_result.stdout.strip()
        display_name = archive_name
        language = "archive"
    else:
        # 单文件
        size_result = await worker.exec_command(f"stat -c%s {q_target}")
        try:
            file_size = int(size_result.stdout.strip())
        except ValueError:
            file_size = 0

        if file_size > _MAX_DOWNLOAD_SIZE:
            return f"文件过大（{file_size // 1024 // 1024}MB），超过 50MB 限制。"

        b64_result = await worker.exec_command(f"base64 -w0 {q_target}", timeout=60)
        content_b64 = b64_result.stdout.strip()
        display_name = path.rsplit("/", 1)[-1] if "/" in path else path
        from db.artifact_store import detect_language
        language = detect_language(display_name)

    if not content_b64:
        return "文件读取失败（内容为空）"

    # ── 保存到 artifacts 表 ──────────────────────────────────────────────────
    # 关键 bug 修复：单文件下载与 sandbox_write 使用同一 path 做 UPSERT。
    # 原逻辑一律存 JSON{binary_b64,...}，会覆盖 sandbox_write 先前保存的
    # 原始 HTML/JS/TXT 内容，导致刷新后前端把 JSON 当 HTML 渲染，出现
    # 一堆 base64 乱码。
    #
    # 正确做法：文本可安全 UTF-8 解码时保存原始文本（与 sandbox_write 一致，
    #         UPSERT 等价于幂等更新）；二进制或解码失败再走 JSON 打包。
    import base64 as _b64
    import json
    raw_bytes: bytes | None = None
    saved_content: str
    is_binary_payload: bool

    if language in ("pptx", "pdf", "archive"):
        # 明确二进制：直接 JSON 打包
        saved_content = json.dumps(
            {"binary_b64": content_b64, "original_size": file_size},
            ensure_ascii=False,
        )
        is_binary_payload = True
    else:
        try:
            raw_bytes = _b64.b64decode(content_b64)
            decoded = raw_bytes.decode("utf-8")
            # NUL 字节：PostgreSQL UTF-8 列不支持，且几乎必然是二进制
            if "\x00" in decoded:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL in decoded text")
            saved_content = decoded
            is_binary_payload = False
        except (UnicodeDecodeError, ValueError):
            saved_content = json.dumps(
                {"binary_b64": content_b64, "original_size": file_size},
                ensure_ascii=False,
            )
            is_binary_payload = True

    artifact = await save_artifact(
        conv_id, display_name, path, saved_content,
        language=language, message_id=msg_id, size=file_size,
    )

    # 通知前端显示下载卡片（带 id，前端下载按钮依赖它）
    await adispatch_custom_event("file_artifact", {
        "id": artifact.get("id", 0),
        "name": display_name,
        "path": path,
        "language": language,
        "size": file_size,
        "binary": is_binary_payload,
        "downloadable": True,
        "message_id": msg_id,
    })

    size_display = f"{file_size / 1024:.1f}KB" if file_size < 1024 * 1024 else f"{file_size / 1024 / 1024:.1f}MB"
    return f"文件已准备好下载: {display_name} ({size_display})。用户可在文件卡片中点击下载。"
