"""
工具注册中心 —— 基于 SkillRegistry 的统一管理

═══════════════════════════════════════════════════════════════
所有注册统一在 main.py lifespan 中按顺序执行：
  1. discover("tools.builtin")     — 注册内置工具
  2. discover("tools.sandboxed")   — 沙箱就绪后注册沙箱工具
  3. MCP 加载后走 register_mcp_tools()

扩展指南（只需 1 步）：
  在对应目录下新建 .py 文件，包含 @tool 函数 + GUIDANCE + ERROR_HINT。
  目录即分类：
    tools/builtin/    — 不依赖外部服务，启动时注册
    tools/sandboxed/  — 依赖沙箱 SSH，就绪后注册
═══════════════════════════════════════════════════════════════
"""
import logging

from langchain_core.tools import BaseTool

from tools.skill import SkillRegistry

logger = logging.getLogger("tools")

_registry = SkillRegistry.instance()


# ── 对外接口 ─────────────────────────────────────────────────────────────────

def discover(package_name: str) -> int:
    """扫描指定包目录，注册其中的 @tool 函数。"""
    return _registry.discover(package_name)


def register_tool(tool: BaseTool, guidance: str = "", error_hint: str = "") -> None:
    """手动注册单个工具。"""
    mod = getattr(tool, "__module__", "")
    if mod and not guidance:
        import importlib
        try:
            m = importlib.import_module(mod)
            guidance = getattr(m, "GUIDANCE", "")
            error_hint = error_hint or getattr(m, "ERROR_HINT", "")
        except Exception:
            pass
    _registry.register(tool, guidance=guidance, error_hint=error_hint)


def unregister_tool(tool_name: str) -> bool:
    return _registry.unregister(tool_name)


def get_all_tools() -> list[BaseTool]:
    return _registry.get_all_tools()


def get_tool_names() -> list[str]:
    return _registry.get_tool_names()


def get_tools_info() -> list[dict]:
    return _registry.get_tools_info()


def get_tools_guidance() -> str:
    return _registry.build_guidance()
