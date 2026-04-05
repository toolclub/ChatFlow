"""
Runner 工具函数

提供整个 runner 包共用的底层工具函数，避免在多个模块中重复定义。
"""
import json


def sse(payload: dict) -> str:
    """
    将 dict 序列化为 SSE 数据行。

    格式：data: {...}\\n\\n
    前端通过 EventSource 接收，每个事件以双换行结束。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def extract_tool_output(output: object) -> str:
    """
    从工具输出对象中提取字符串内容。

    兼容 LangChain ToolMessage、纯字符串和其他类型。
    """
    if hasattr(output, "content"):
        content = output.content
        return content if isinstance(content, str) else str(content)
    return str(output)
