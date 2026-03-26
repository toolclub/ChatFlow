"""
内置工具注册表

扩展指南：
  1. 在 tools/builtin/ 目录下创建新文件
  2. 使用 @tool 装饰器定义工具函数
  3. 在此文件的 BUILTIN_TOOLS 列表中追加即可，无需修改其他代码

示例（tools/builtin/my_tool.py）：
    from langchain_core.tools import tool

    @tool
    def my_tool(param: str) -> str:
        \"\"\"工具描述，LLM 会读这段描述来决定何时使用此工具。\"\"\"
        return f"结果: {param}"

然后在此文件加一行：
    from tools.builtin.my_tool import my_tool
    BUILTIN_TOOLS = [..., my_tool]
"""
from tools.builtin.calculator import calculator
from tools.builtin.datetime_tool import get_current_datetime
from tools.builtin.fetch_webpage import fetch_webpage
from tools.builtin.web_search import web_search

BUILTIN_TOOLS = [
    calculator,
    get_current_datetime,
    web_search,
    fetch_webpage,
]
