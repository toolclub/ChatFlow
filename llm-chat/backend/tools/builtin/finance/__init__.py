"""finance 工具子包 — 财经数据原子工具

设计：
  - 每个工具是 quant.data.* 的轻包装，把数据序列化为 JSON 字符串
  - 仅在 finance 路由命中时通过 `call_model_node` 工具绑定层暴露给 LLM
    （build_guidance 现策略仅区分 chat / 非 chat，精细路由隔离在工具绑定层做）

注册路径（main.py lifespan）：
    discover("tools.builtin.finance")
"""
