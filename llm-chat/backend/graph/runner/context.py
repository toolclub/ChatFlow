"""
StreamContext：流式会话上下文

保存当前 SSE 流处理过程中的可变状态，在 EventDispatcher 和各 EventHandler 间共享。
每次 stream_response 调用创建一个独立实例，互不干扰。
"""
from dataclasses import dataclass, field


@dataclass
class StreamContext:
    """
    流式会话级可变状态。

    active_model:        当前激活的模型名称（路由决策后更新）
    compressed:          本轮是否触发了记忆压缩（流结束时写入 done 事件）
    last_plan_step_count: 最近一次计划的步骤数（用于 step_update 事件）
    in_think_block:      当前是否处于 <think> 推理块内（跨 chunk 的状态追踪）
    after_tool_streamed: call_model_after_tool 是否已通过流式发送过 token
    call_model_streamed: call_model 是否已通过流式发送过 token

    streamed 标志用于避免重复发送：
      - 若流式 token 已发 → CallModelEndHandler 跳过补发
      - 若未收到流式 token → CallModelEndHandler 从 on_chain_end 补发完整内容
    """
    active_model: str
    compressed: bool = False
    last_plan_step_count: int = 0
    in_think_block: bool = False
    after_tool_streamed: bool = False
    call_model_streamed: bool = False
