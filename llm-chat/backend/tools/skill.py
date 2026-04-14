"""
Skill 框架 — 工具的自包含化 + 自动发现 + 统一错误处理

设计目标：
  新增一个 skill 只需 1 步：
    在 tools/builtin/ 下新建 .py 文件，写 @tool 函数 + GUIDANCE 常量。
    不需要改 __init__.py、不需要改 system.md、不需要改 main.py。

三层能力：
  1. 自动发现：扫描 tools/builtin/ 目录，收集所有 @tool 函数
  2. Prompt 就近放：每个工具文件的 GUIDANCE 常量自动注入 system prompt
  3. 统一错误处理：包装所有工具的执行方法，异常时返回结构化错误 + 恢复建议

兼容性：
  - 输出仍是 LangChain BaseTool 列表，LangGraph ToolNode 无感知
  - MCP 工具和动态工具（sandbox）走原有路径，不受影响
"""
import asyncio
import importlib
import inspect
import logging
import pkgutil
import time
from functools import wraps
from typing import Any

from langchain_core.tools import BaseTool

logger = logging.getLogger("tools.skill")


# ══════════════════════════════════════════════════════════════════════════════
# SkillMeta：每个 skill 的元数据
# ══════════════════════════════════════════════════════════════════════════════

class SkillMeta:
    """技能元数据 — 与 BaseTool 实例关联"""

    __slots__ = ("name", "guidance", "error_hint", "source", "tags", "display_mode")

    def __init__(
        self,
        name: str,
        guidance: str = "",
        error_hint: str = "请检查参数后重试",
        source: str = "builtin",
        tags: list[str] | None = None,
        display_mode: str = "default",
    ):
        self.name = name
        self.guidance = guidance.strip()
        self.display_mode = display_mode
        self.error_hint = error_hint.strip()
        self.source = source
        self.tags = tags or []

    def __repr__(self) -> str:
        return f"SkillMeta({self.name!r}, source={self.source!r})"


# ══════════════════════════════════════════════════════════════════════════════
# SkillRegistry：单例注册表
# ══════════════════════════════════════════════════════════════════════════════

class SkillRegistry:
    """
    技能注册表 — 自动发现、统一管理、prompt 收集

    使用方式：
        registry = SkillRegistry.instance()
        registry.discover_builtin()          # 启动时调用一次
        tools = registry.get_all_tools()     # 获取 LangChain 工具列表
        guidance = registry.build_guidance()  # 获取 system prompt 片段
    """

    _instance: "SkillRegistry | None" = None

    def __init__(self):
        self._tools: dict[str, tuple[BaseTool, SkillMeta]] = {}  # name → (BaseTool, SkillMeta)
        self._mcp: list[BaseTool] = []

    @classmethod
    def instance(cls) -> "SkillRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 发现与注册 ─────────────────────────────────────────────────────────

    def discover(self, package_name: str) -> int:
        """
        扫描指定包下的所有模块，注册其中的 @tool 函数。

        目录即分类，不需要任何标记：
          discover("tools.builtin")   → 注册内置工具
          discover("tools.sandbox")   → 注册沙箱工具

        协议（每个工具文件可定义的常量）：
          - GUIDANCE: str   → 注入 system prompt 的使用指导
          - ERROR_HINT: str → 失败时给 LLM 的恢复建议
          - TAGS: list[str] → 分类标签

        返回本次注册的工具数量。
        """
        package = importlib.import_module(package_name)
        registered = 0

        for finder, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
            if is_pkg or module_name.startswith("_"):
                continue
            full_name = f"{package_name}.{module_name}"
            try:
                mod = importlib.import_module(full_name)
            except Exception as exc:
                logger.warning("跳过模块 %s（导入失败）: %s", full_name, exc)
                continue

            guidance = getattr(mod, "GUIDANCE", "")
            error_hint = getattr(mod, "ERROR_HINT", "请检查参数后重试")
            tags = getattr(mod, "TAGS", [])
            # DISPLAY_MODE: 单工具模块用 str，多工具模块用 DISPLAY_MODES dict
            display_mode_default = getattr(mod, "DISPLAY_MODE", "default")
            display_modes_map = getattr(mod, "DISPLAY_MODES", {})

            found = 0
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, BaseTool) and obj.name not in self._tools:
                    mode = display_modes_map.get(obj.name, display_mode_default)
                    meta = SkillMeta(
                        name=obj.name,
                        guidance=guidance,
                        error_hint=error_hint,
                        source=package_name.rsplit(".", 1)[-1],
                        tags=tags,
                        display_mode=mode,
                    )
                    wrapped = _wrap_error_handling(obj, meta)
                    self._tools[obj.name] = (wrapped, meta)
                    found += 1
                    registered += 1

        if registered:
            logger.info("工具注册 | package=%s | +%d 个 | total=%d", package_name, registered, len(self._tools))
        return registered

    # ── 手动注册（MCP / 动态） ───────────────────────────────────────────────

    def register_mcp_tools(self, tools: list[BaseTool]) -> None:
        """注册 MCP 工具列表。"""
        self._mcp = tools

    def register(
        self,
        tool: BaseTool,
        guidance: str = "",
        error_hint: str = "请检查参数后重试",
        tags: list[str] | None = None,
    ) -> None:
        """手动注册单个工具。"""
        meta = SkillMeta(
            name=tool.name, guidance=guidance, error_hint=error_hint,
            source="manual", tags=tags or [],
        )
        wrapped = _wrap_error_handling(tool, meta)
        self._tools[tool.name] = (wrapped, meta)
        logger.info("手动注册工具: %s", tool.name)

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            logger.info("已移除工具: %s", name)
            return True
        return False

    def get_display_mode(self, tool_name: str) -> str:
        """查询工具的渲染模式（供 SSE handler 调用）。"""
        entry = self._tools.get(tool_name)
        if entry:
            return entry[1].display_mode
        return "default"

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def get_all_tools(self) -> list[BaseTool]:
        """返回所有可用工具（discover 注册的 + MCP）。"""
        result: list[BaseTool] = [t for t, _ in self._tools.values()]
        result.extend(self._mcp)
        return result

    def get_tool_names(self) -> list[str]:
        return [t.name for t in self.get_all_tools()]

    def get_tools_info(self) -> list[dict]:
        result: list[dict] = []
        for t, meta in self._tools.values():
            result.append({
                "name": t.name, "description": t.description or "",
                "source": meta.source, "tags": meta.tags,
            })
        for t in self._mcp:
            result.append({"name": t.name, "description": t.description or "", "source": "mcp"})
        return result

    # ── Prompt 收集 ──────────────────────────────────────────────────────────

    def build_guidance(self) -> str:
        """收集所有工具的 GUIDANCE，拼接为 system prompt 片段。"""
        parts: list[str] = []
        for tool, meta in self._tools.values():
            if meta.guidance:
                parts.append(f"▸ {tool.name}: {meta.guidance}")
        if not parts:
            return ""
        return "【可用工具指南】\n" + "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 统一错误处理包装器
# ══════════════════════════════════════════════════════════════════════════════

def _wrap_error_handling(tool: BaseTool, meta: SkillMeta) -> BaseTool:
    """
    为 BaseTool 的执行方法包装统一错误处理。

    效果：
      - 异常不会传播到 LangGraph（ToolNode 收到的永远是字符串）
      - LLM 能看到结构化的错误信息和恢复建议
      - 超时有明确提示
    """
    original_arun = tool.coroutine  # @tool 装饰器的 async 函数

    if original_arun is None:
        # 同步工具，不包装
        return tool

    @wraps(original_arun)
    async def safe_arun(*args: Any, **kwargs: Any) -> str:
        start = time.monotonic()
        try:
            result = await original_arun(*args, **kwargs)
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning(
                "工具超时 | name=%s | elapsed=%.1fs", meta.name, elapsed,
            )
            return (
                f"⚠️ 工具 {meta.name} 执行超时（{elapsed:.0f}s）。\n"
                f"建议: {meta.error_hint}"
            )
        except asyncio.CancelledError:
            raise  # 不拦截取消信号
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "工具异常 | name=%s | elapsed=%.1fs | error=%s",
                meta.name, elapsed, exc,
            )
            return (
                f"⚠️ 工具 {meta.name} 执行失败: {exc}\n"
                f"建议: {meta.error_hint}"
            )

    tool.coroutine = safe_arun
    return tool
