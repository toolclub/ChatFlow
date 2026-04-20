"""
沙箱执行上下文：通过 contextvars 在图执行期间传递 conv_id 给沙箱工具。

设计原因：
  LangGraph ToolNode 执行工具时，@tool 函数的 config 参数注入不稳定。
  使用 contextvars 在图执行入口设置 conv_id，工具函数内直接读取。
  这是 Python 标准的异步上下文传递方式，线程/协程安全。
"""
from contextvars import ContextVar
from typing import Optional

# 当前执行的对话 ID（在 stream.py 的 _run_graph 入口设置）
current_conv_id: ContextVar[str] = ContextVar("sandbox_conv_id", default="")
# 当前 assistant 消息 ID（用于 artifact 关联）
current_message_id: ContextVar[str] = ContextVar("sandbox_message_id", default="")

# 本轮的澄清数据槽位。
#
# 关键设计：ContextVar 存储的是一个 **可变 dict 容器**，不是澄清数据本身。
#   stream.py 每轮入口用 set({}) 放入一个新 dict；
#   request_clarification 工具向 dict 写入 {"data": payload}；
#   save_response_node 从同一 dict 读取 data 并清空。
#
# 为什么要这样做？
#   Python `asyncio.Task` 在创建时拷贝 contextvars 快照，子 Task 中调用
#   `.set()` 只修改子 Task 自己的副本，不会回写到父 Task。LangGraph 的
#   ToolNode 通过 asyncio.gather 启动 Task 执行工具，若工具内 .set()
#   新 dict，父节点（save_response_node）读到的仍是最初的 None。
#
#   因此 ContextVar 只在 stream.py 入口 .set() 一次，之后所有读写都是
#   对 **同一个 dict 的 in-place 变更**——Task 之间共享 dict 引用，
#   变更天然可见。`None` 用于"未初始化"保护，防止默认 dict 被误复用。
current_clarification: ContextVar[Optional[dict]] = ContextVar(
    "clarification_slot", default=None,
)
