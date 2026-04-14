"""
内置工具：安全数学计算器
使用 AST 解析替代 eval()，只允许基础算术运算。
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = "需要精确数学计算时使用（加减乘除、幂、取余）。不支持函数调用（sin/cos/log 等），复杂数学请用 execute_code。"
ERROR_HINT = "表达式格式错误，请简化为基础算术运算（+、-、*、/、**、%）。"
TAGS = ["utility"]
DISPLAY_MODE = "default"  # 非终端工具，用默认渲染
import ast
import operator

from langchain_core.tools import tool

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str) -> float:
    tree = ast.parse(expr.strip(), mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _SAFE_OPS:
                raise ValueError(f"不支持的运算符: {op_type.__name__}")
            return _SAFE_OPS[op_type](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _SAFE_OPS:
                raise ValueError(f"不支持的运算符: {op_type.__name__}")
            return _SAFE_OPS[op_type](_eval(node.operand))
        raise ValueError(f"不支持的表达式类型: {type(node).__name__}")

    return _eval(tree)


@tool
def calculator(expression: str) -> str:
    """
    计算数学表达式，支持加(+)、减(-)、乘(*)、除(/)、整除(//)、乘方(**)、取模(%)。

    Args:
        expression: 数学表达式，例如 "2 + 3 * 4" 或 "100 / 7" 或 "2 ** 10"

    Returns:
        计算结果字符串，例如 "2 + 3 * 4 = 14.0"
    """
    try:
        result = _safe_eval(expression)
        # 整数结果去掉小数点
        display = int(result) if result == int(result) else result
        return f"{expression} = {display}"
    except ZeroDivisionError:
        return "错误：除数不能为零"
    except Exception as exc:
        return f"计算失败: {exc}"
