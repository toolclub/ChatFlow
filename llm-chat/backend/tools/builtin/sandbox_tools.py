"""
沙箱代码执行工具（流式输出版）

核心改进：代码执行过程中，stdout/stderr 实时通过 adispatch_custom_event 推送给前端。
前端终端面板逐行渲染，用户能看到程序运行的完整过程（而非等结束后一次性输出）。

事件类型：
  sandbox_output: {"stream": "stdout"|"stderr", "text": "...", "tool_name": "execute_code"}
  → 前端在 tool_call 区域实时追加输出文本
"""
import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger("tools.sandbox")


def _get_conv_id() -> str:
    from sandbox.context import current_conv_id
    return current_conv_id.get() or "default"


@tool
async def execute_code(language: str, code: str) -> str:
    """
    在安全沙箱环境中执行代码。支持 Python、JavaScript、Java、Shell。
    每个对话有独立的工作目录，可以读写文件、安装包。
    执行过程中输出会实时显示在终端中。

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

    result = await sandbox_manager.execute_code_streaming(
        conv_id, language, code,
        on_output=lambda stream, text: adispatch_custom_event(
            "sandbox_output",
            {"stream": stream, "text": text, "tool_name": "execute_code"},
        ),
    )
    return result.to_display()


@tool
async def run_shell(command: str) -> str:
    """
    在沙箱环境中执行 shell 命令。工作目录为当前对话的专属 session 目录。
    执行过程中输出会实时显示在终端中。

    Args:
        command: 要执行的 shell 命令

    Returns:
        命令输出结果
    """
    from langchain_core.callbacks.manager import adispatch_custom_event
    from sandbox.manager import sandbox_manager

    conv_id = _get_conv_id()
    logger.info("run_shell | conv=%s | cmd='%.100s'", conv_id, command)

    result = await sandbox_manager.run_shell_streaming(
        conv_id, command,
        on_output=lambda stream, text: adispatch_custom_event(
            "sandbox_output",
            {"stream": stream, "text": text, "tool_name": "run_shell"},
        ),
    )
    return result.to_display()


@tool
async def sandbox_write(path: str, content: str) -> str:
    """
    在沙箱工作目录中创建或写入文件。路径相对于当前 session 目录。

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
    logger.info("sandbox_write | conv=%s | path=%s | len=%d", conv_id, path, len(content))
    result = await sandbox_manager.write_file(conv_id, path, content)

    # 写入完成后，保存为文件产物并通知前端（write_file 内部总是 exit_code=0）
    try:
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        await save_artifact(conv_id, name, path, content)
        await adispatch_custom_event(
            "file_artifact",
            {"name": name, "path": path, "content": content, "language": detect_language(path)},
        )
    except Exception:
        logger.warning("file_artifact 事件发送失败 | conv=%s path=%s", conv_id, path, exc_info=True)

    return result.to_display()


@tool
async def sandbox_read(path: str) -> str:
    """
    读取沙箱工作目录中的文件内容。路径相对于当前 session 目录。

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
