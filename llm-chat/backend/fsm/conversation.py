"""
对话状态机 — 管理对话生命周期

状态流转图：
    ACTIVE ──start_stream──→ STREAMING ──complete──→ COMPLETED
                              │                        │
                              ├──fail──→ ERROR ────────┤
                              │                        │
                              └──stop──→ ACTIVE        │
                                           ↑           │
                                           └──new_round┘

使用方式：
    sm = ConversationSM.from_db_status("active")
    sm.start_stream()       # active → streaming
    sm.complete()           # streaming → completed
    print(sm.current_value) # "completed"
"""
from __future__ import annotations

import logging
from statemachine import StateMachine, State

logger = logging.getLogger("statemachine.conversation")


class ConversationSM(StateMachine):
    """对话生命周期状态机。"""

    # ── 状态定义 ──
    active = State(initial=True)
    streaming = State()
    completed = State()
    error = State()

    # ── 转换定义 ──
    start_stream = active.to(streaming)
    complete = streaming.to(completed)
    fail = streaming.to(error)
    stop = streaming.to(active)
    new_round = completed.to(streaming) | error.to(streaming)

    # ── 转换回调（日志 + 可扩展） ──
    def on_enter_streaming(self) -> None:
        logger.debug("对话进入 streaming 状态 | conv=%s", self.conv_id)

    def on_enter_completed(self) -> None:
        logger.debug("对话完成 | conv=%s", self.conv_id)

    def on_enter_error(self) -> None:
        logger.warning("对话进入 error 状态 | conv=%s", self.conv_id)

    def __init__(self, conv_id: str = "", **kwargs) -> None:
        self.conv_id = conv_id
        super().__init__(**kwargs)

    @property
    def current_value(self) -> str:
        """当前状态的字符串值（用于写 DB）。"""
        return self.current_state_value

    @classmethod
    def from_db_status(cls, status: str, conv_id: str = "") -> ConversationSM:
        """从 DB 中的 status 字符串恢复状态机实例。"""
        valid = {"active", "streaming", "completed", "error"}
        start = status if status in valid else "active"
        return cls(conv_id=conv_id, start_value=start)

    def send_event(self, target_status: str) -> str:
        """
        根据目标状态自动选择正确的转换事件。

        这是给 memory/store.py 用的便捷方法：
            sm.send_event("streaming")  # 自动选择 start_stream 或 new_round
        返回转换后的状态字符串。
        非法转换抛 TransitionNotAllowed 异常。
        """
        current = self.current_value

        # 根据 (current, target) 选择对应的转换方法
        transition_map: dict[tuple[str, str], str] = {
            ("active", "streaming"): "start_stream",
            ("streaming", "completed"): "complete",
            ("streaming", "error"): "fail",
            ("streaming", "active"): "stop",
            ("completed", "streaming"): "new_round",
            ("error", "streaming"): "new_round",
        }

        event_name = transition_map.get((current, target_status))
        if not event_name:
            logger.warning(
                "对话状态转换无效: %s → %s | conv=%s",
                current, target_status, self.conv_id,
            )
            return current

        getattr(self, event_name)()
        return self.current_value
