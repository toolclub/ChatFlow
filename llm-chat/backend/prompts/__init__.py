"""
提示词管理模块

目录结构：
    prompts/
    ├── system.md                      # 主助手人格 & 行为规范
    ├── summary.md                     # 对话压缩指令
    ├── nodes/
    │   ├── route.md                   # 路由分类提示词
    │   ├── planner.md                 # 任务规划提示词（支持 {{today}} 变量）
    │   ├── reflector.md               # 步骤评估提示词
    │   ├── call_model_step.md         # 中间步骤执行者提示词
    │   ├── after_tool_step.md         # 工具结果摘要提示词
    │   └── compressor.md              # 压缩更新指令
    └── clarification/
        └── webpage.json               # 网页生成澄清卡片

使用方式：
    from prompts import load_prompt, load_json_prompt

    # 加载纯文本提示词
    system = load_prompt("system")
    planner = load_prompt("nodes/planner", today="2026年4月7日")

    # 加载 JSON 提示词
    card = load_json_prompt("clarification/webpage")

扩展：新增节点只需在 nodes/ 下添加对应 .md 文件，代码中 load_prompt("nodes/xxx") 即可。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("prompts")

_DIR = Path(__file__).parent
_cache: dict[str, str] = {}


def load_prompt(name: str, **variables) -> str:
    """
    加载提示词文件并替换模板变量。

    参数：
        name:       相对于 prompts/ 的文件名（不含 .md 后缀），如 "nodes/route"
        **variables: 模板变量，替换文件中的 {{key}} 占位符

    示例：
        load_prompt("nodes/planner", today="2026年4月7日")
    """
    if name not in _cache:
        path = _DIR / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"提示词文件不存在: {path}")
        _cache[name] = path.read_text(encoding="utf-8").strip()
        logger.debug("已加载提示词 %s (%d chars)", name, len(_cache[name]))

    text = _cache[name]
    for key, value in variables.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text


def load_json_prompt(name: str) -> dict:
    """
    加载 JSON 格式提示词（如澄清卡片模板）。

    参数：
        name: 相对于 prompts/ 的文件名（不含 .json 后缀），如 "clarification/webpage"
    """
    cache_key = f"__json__{name}"
    if cache_key not in _cache:
        path = _DIR / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"JSON 提示词文件不存在: {path}")
        _cache[cache_key] = path.read_text(encoding="utf-8").strip()
        logger.debug("已加载 JSON 提示词 %s", name)
    return json.loads(_cache[cache_key])


def reload():
    """清空缓存，强制重新加载所有提示词（热更新用）。"""
    _cache.clear()
    logger.info("提示词缓存已清空")
