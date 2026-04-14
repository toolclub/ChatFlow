"""
沙箱工具包 — 依赖沙箱 SSH 连接，由 main.py 在沙箱就绪后注册

目录名用 sandboxed 而非 sandbox，避免与顶层 sandbox 包（SSH Manager）的 import 冲突。

扩展指南：
  在此目录下新建 .py 文件，包含 @tool 函数 + GUIDANCE + ERROR_HINT。
  沙箱就绪后 discover("tools.sandboxed") 自动注册。
"""
