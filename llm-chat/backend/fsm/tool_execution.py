"""
工具执行状态机 — 管理单次工具调用的生命周期

状态流转图：
    RUNNING ──finish──→ DONE
            ──fail────→ ERROR
            ──timeout─→ TIMEOUT

使用方式：
    sm = ToolExecutionSM()
    sm.finish()             # running → done
    print(sm.current_value) # "done"
"""
from __future__ import annotations

import logging
from statemachine import StateMachine, State

logger = logging.getLogger("statemachine.tool_execution")


class ToolExecutionSM(StateMachine):
    """工具执行生命周期状态机。"""

    # ── 状态定义 ──
    running = State(initial=True)
    done = State(final=True)
    error = State(final=True)
    timeout = State(final=True)

    # ── 转换定义 ──
    finish = running.to(done)
    fail = running.to(error)
    expire = running.to(timeout)

    # ── 回调 ──
    def on_enter_error(self) -> None:
        logger.debug("工具执行失败")

    def on_enter_timeout(self) -> None:
        logger.debug("工具执行超时")

    @property
    def current_value(self) -> str:
        """当前状态的字符串值（用于写 DB）。"""
        return self.current_state_value

    def send_event(self, target_status: str) -> str:
        """
        根据目标状态自动选择转换事件。

        返回转换后的状态字符串。非法转换抛 TransitionNotAllowed。
        """
        transition_map: dict[str, str] = {
            "done": "finish",
            "error": "fail",
            "timeout": "expire",
        }
        event_name = transition_map.get(target_status)
        if not event_name:
            logger.warning("工具状态转换无效: %s → %s", self.current_value, target_status)
            return self.current_value

        getattr(self, event_name)()
        return self.current_value
