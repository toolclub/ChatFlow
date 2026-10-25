"""
股票预测任务状态机 — 管理单次预测的生命周期

状态流转图（v2 新增 paused）：
                  ┌──────────── resume* ────────────┐
                  ↓                                  │
    queued ─start_news→ news_searching ─start_infer→ inferring ─finish→ done
      │                      │                           │
      ├──── pause ───────────┼──────────── pause ────────┤
      │                      ↓                           │
      └───────────────→   paused  ←──────────────────────┘
      │                      │
      ├──── fail ────────────┼──── fail ────→ failed  (final)
      └──── stop ────────────┴──── stop ────→ stopped (final)

★ paused **不是终态**（stopped 才是）。语义 = 取消 + 保留已完成维度 + 续跑剩余维度，
  不是"原地冻结再解冻"—— 全仓库没有 LangGraph checkpointer，做不到真冻结（设计文档 §7.2）。
  恢复时编排层读回已完成维度，只把剩余维度重新跑一遍。

resume 有三个落点，各自是**独立的单目标事件**（不能写成一个多目标 resume：
python-statemachine 会静默取第一个可行目标，导致 send_event("inferring") 落到 queued）：
    resume       → queued          （重新走整张图，设计文档 §7.1 的主路径）
    resume_news  → news_searching  （还有资讯维度没跑完，续跑剩余维度）
    resume_infer → inferring       （维度全跑完了，直接进推理）

使用方式：
    sm = PredictionSM()
    sm.send_event("news_searching")   # queued → news_searching
    sm.send_event("paused")           # news_searching → paused
    sm.send_event("news_searching")   # paused → news_searching（自动选 resume_news）
    sm.send_event("inferring")        # news_searching → inferring
    sm.send_event("done")             # inferring → done
    print(sm.current_value)           # "done"

    sm = PredictionSM.from_db_status("inferring")
    sm.send_event("failed")           # inferring → failed

铁律 #6：状态变更先走状态机校验，再写 DB。status 存 PredictionStatus 枚举值（真 (str, Enum)，
非字符串字面量——修正 quant 用 COMPUTING/DONE/FAILED 字面量的既有违规）。
"""
from __future__ import annotations

import logging
from enum import Enum
from statemachine import StateMachine, State

logger = logging.getLogger("statemachine.prediction")


class PredictionStatus(str, Enum):
    """预测任务状态。

    DB prediction_snapshots.status 存枚举值；前端按此着色（排队中/资讯搜索中/模型推理中/已完成/失败）。
    继承 (str, Enum) 使 JSON 序列化与 DB 存储无需转换（与 SSEEventType 同风格，教训见 spec「踩过的坑」）。
    """

    QUEUED = "queued"                  # 排队中
    NEWS_SEARCHING = "news_searching"  # 资讯搜索中
    INFERRING = "inferring"            # 模型推理中
    PAUSED = "paused"                  # 已暂停（非终态；取消在途任务 + 保留已完成维度，可 resume 续跑）
    DONE = "done"                      # 已完成
    FAILED = "failed"                  # 失败
    STOPPED = "stopped"                # 已停止（用户主动停止；取消 graph_task + astream + LLM HTTP 流）


# 目标状态 → 候选转换事件（按顺序试，第一个当前允许的生效）。
# 一个目标可能有多个来源事件（如 news_searching 既可来自 queued 的 start_news，
# 也可来自 paused 的 resume_news），所以是元组而不是单个字符串。
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("resume", "recover"),
    "news_searching": ("start_news", "resume_news"),
    "inferring": ("start_infer", "resume_infer"),
    "paused": ("pause",),
    "done": ("finish",),
    "failed": ("fail",),
    "stopped": ("stop",),
}


class PredictionSM(StateMachine):
    """预测任务生命周期状态机。转换由编排层调用，校验通过后才持久化（铁律 #6）。"""

    # ── 状态定义 ──
    queued = State(initial=True)
    news_searching = State()
    inferring = State()
    paused = State()                   # ★ 非 final：暂停后可 resume 续跑
    done = State(final=True)
    failed = State(final=True)
    stopped = State(final=True)

    # ── 转换定义 ──
    start_news = queued.to(news_searching)
    start_infer = news_searching.to(inferring)
    finish = inferring.to(done)
    # 任一运行态 → paused（用户暂停；取消在途任务，已完成维度已增量落库）
    pause = queued.to(paused) | news_searching.to(paused) | inferring.to(paused)
    # paused → 运行态（续跑）。三个独立单目标事件，避免多目标歧义（见模块 docstring）。
    resume = paused.to(queued)
    resume_news = paused.to(news_searching)
    resume_infer = paused.to(inferring)
    # Only the worker that acquired an expired/missing ownership lease may
    # restart an interrupted graph. Completed dimensions remain in the DB.
    recover = news_searching.to(queued) | inferring.to(queued)
    # 任一运行态（含 paused）→ failed（python-statemachine 多源到单目标的 | 写法）
    fail = (
        queued.to(failed) | news_searching.to(failed) | inferring.to(failed)
        | paused.to(failed)
    )
    # 任一运行态（含 paused）→ stopped（用户主动停止；终态。取消后台图 + astream + LLM HTTP 流）
    stop = (
        queued.to(stopped) | news_searching.to(stopped) | inferring.to(stopped)
        | paused.to(stopped)
    )

    @property
    def current_value(self) -> str:
        """当前状态的字符串值（用于写 DB）。"""
        return self.current_state_value

    @classmethod
    def from_db_status(cls, status: str) -> "PredictionSM":
        """从 DB 中的 status 字符串恢复状态机实例。"""
        valid = {s.value for s in PredictionStatus}
        start = status if status in valid else "queued"
        return cls(start_value=start)

    def send_event(self, target_status: str) -> str:
        """
        按目标状态自动选择转换事件，返回转换后的状态字符串。

        一个目标可能对应多个来源事件（如 news_searching ← start_news / resume_news），
        按 _TRANSITIONS 的顺序逐个试，第一个当前允许的生效。

        非法转换记 warning 并返回当前值（铁律 #9：异常不吞，但不崩编排层）。
        ★ 调用方注意：本方法对非法转换**不抛异常**，而是原样返回当前状态。
          编排层必须比较返回值与目标值，不能盲写返回值（否则任务会永远卡在旧状态）。
        """
        candidates = _TRANSITIONS.get(target_status)
        if not candidates:
            logger.warning("预测状态转换无效: %s → %s", self.current_value, target_status)
            return self.current_value

        last_exc: Exception | None = None
        for event_name in candidates:
            try:
                getattr(self, event_name)()
                return self.current_value
            except Exception as exc:
                # 该候选事件在当前状态不允许，试下一个
                last_exc = exc

        # 所有候选都不允许：记 warning，保留当前状态（编排层据此决定是否强制置 failed）
        logger.warning(
            "预测状态转换异常: %s → %s err=%s", self.current_value, target_status, last_exc,
        )
        return self.current_value
