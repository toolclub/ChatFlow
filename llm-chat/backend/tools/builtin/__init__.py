"""
内置工具包 — 由 SkillRegistry 自动发现

扩展指南（只需 1 步）：
  在 tools/builtin/ 目录下创建新 .py 文件，包含：
    1. @tool 装饰器的函数（会被自动发现和注册）
    2. GUIDANCE 常量（可选，注入 system prompt 的使用指导）
    3. ERROR_HINT 常量（可选，失败时给 LLM 的恢复建议）
    4. TAGS 常量（可选，分类标签）

  无需修改任何其他文件。

示例（tools/builtin/my_tool.py）：
    from langchain_core.tools import tool

    GUIDANCE = "当用户需要 XXX 时使用此工具。"
    ERROR_HINT = "请检查参数格式后重试。"
    TAGS = ["utility"]

    @tool
    async def my_tool(param: str) -> str:
        \"\"\"工具描述（LLM 读此描述决定何时调用）。\"\"\"
        return f"结果: {param}"

沙箱工具不在此目录自动发现——由 main.py lifespan 中 SSH 连接成功后动态注册。
原因：沙箱可能未部署，若 import 时注册，模型会看到工具但调用全部失败。
"""
