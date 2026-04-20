"""
事实级长期记忆的数据结构与 Qdrant payload 互转。

背景：
    旧版把"一轮 Q&A 文本对"整体存进 Qdrant，检索出来的是完整对话
    片段，噪音大、无法 UPDATE/DELETE。新版以"事实 fact"为最小单元，
    每条事实独立存储、独立检索、可被覆盖。

向后兼容（COMPAT）：
    已存在的点仍然是 {user, assistant, msg_idx} 结构。检索时
    `FactRecord.from_payload` 会识别旧结构并构造一个 legacy fact，
    让上层以统一接口处理新旧数据。
    当旧数据全部被压缩/淘汰后可移除 legacy 分支。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ── 事实类型常量（供 extractor 与 prompt 对齐） ──────────────────────────────
FACT_TYPE_PREFERENCE   = "preference"    # 用户偏好（如"默认用深色主题"）
FACT_TYPE_IDENTITY     = "identity"      # 用户身份（如"是前端工程师"）
FACT_TYPE_RULE         = "rule"          # 项目/场景硬约束
FACT_TYPE_GOAL         = "goal"          # 长期目标/当前任务
FACT_TYPE_KNOWLEDGE    = "knowledge"     # 客观事实（用户所处项目、工具栈等）
FACT_TYPE_EVENT        = "event"         # 具体事件（如"2026-04 上线 X 模块"）
FACT_TYPE_RELATIONSHIP = "relationship"  # 人/实体之间的关系
FACT_TYPE_LEGACY_PAIR  = "legacy_pair"   # COMPAT：来自旧 Q&A 对的降级

VALID_FACT_TYPES: tuple[str, ...] = (
    FACT_TYPE_PREFERENCE,
    FACT_TYPE_IDENTITY,
    FACT_TYPE_RULE,
    FACT_TYPE_GOAL,
    FACT_TYPE_KNOWLEDGE,
    FACT_TYPE_EVENT,
    FACT_TYPE_RELATIONSHIP,
)


@dataclass
class FactRecord:
    """
    Qdrant 中一条长期事实的内存表示。

    字段说明：
        fact:                 事实的自然语言表述（embedding 的源文本）
        fact_type:            分类，见 VALID_FACT_TYPES
        confidence:           抽取置信度 [0, 1]，低于阈值不写入
        ts:                   创建或最近一次更新时间戳
        user_id:              所属用户标识（= Conversation.client_id），跨对话检索的过滤键
        conv_id:              首次产生该事实的对话 ID（用于追溯/按会话清理）
        source_msg_id:        产生该事实的 user 消息 DB 自增 ID（0 表示未知）
        source_user_msg:      产生该事实时的用户原话（最多 400 字，用于追溯/展示）
        source_assistant_msg: 同上，对应助手回复（最多 400 字）
        superseded_by:        被哪条事实替代（UPDATE 时写入新 point_id；未被替代则 0）
        legacy:               COMPAT：True 表示由旧版 {user, assistant} payload 构造
    """

    fact: str
    fact_type: str = FACT_TYPE_KNOWLEDGE
    confidence: float = 1.0
    ts: float = field(default_factory=time.time)
    user_id: str = ""
    conv_id: str = ""
    source_msg_id: int = 0
    source_user_msg: str = ""
    source_assistant_msg: str = ""
    superseded_by: int = 0
    legacy: bool = False

    # ── 序列化 ──────────────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        """构建 Qdrant payload（只包含非空字段，减小存储体积）。"""
        payload: dict[str, Any] = {
            "schema":      "fact_v1",
            "fact":        self.fact,
            "fact_type":   self.fact_type,
            "confidence":  round(float(self.confidence), 3),
            "ts":          self.ts,
            "user_id":     self.user_id,
            "conv_id":     self.conv_id,
        }
        if self.source_msg_id:
            payload["source_msg_id"] = int(self.source_msg_id)
        if self.source_user_msg:
            payload["source_user_msg"] = self.source_user_msg[:400]
        if self.source_assistant_msg:
            payload["source_assistant_msg"] = self.source_assistant_msg[:400]
        if self.superseded_by:
            payload["superseded_by"] = int(self.superseded_by)
        return payload

    # ── 反序列化 ───────────────────────────────────────────────────────────

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Optional["FactRecord"]:
        """
        从 Qdrant payload 还原 FactRecord。

        兼容三种结构：
          1. 新版 fact_v1（含 `fact` 字段）
          2. COMPAT 旧版 Q&A 对（含 `user` + `assistant`）
          3. 脏数据（两种都不匹配）→ 返回 None
        """
        if not payload or not isinstance(payload, dict):
            return None

        # 新版
        if payload.get("schema") == "fact_v1" or "fact" in payload:
            fact = (payload.get("fact") or "").strip()
            if not fact:
                return None
            return cls(
                fact=fact,
                fact_type=payload.get("fact_type") or FACT_TYPE_KNOWLEDGE,
                confidence=float(payload.get("confidence") or 1.0),
                ts=float(payload.get("ts") or 0.0),
                user_id=payload.get("user_id") or "",
                conv_id=payload.get("conv_id") or "",
                source_msg_id=int(payload.get("source_msg_id") or 0),
                source_user_msg=payload.get("source_user_msg") or "",
                source_assistant_msg=payload.get("source_assistant_msg") or "",
                superseded_by=int(payload.get("superseded_by") or 0),
                legacy=False,
            )

        # COMPAT：旧版 Q&A payload
        user_txt = (payload.get("user") or "").strip()
        asst_txt = (payload.get("assistant") or "").strip()
        if user_txt or asst_txt:
            legacy_fact = _legacy_pair_to_fact(user_txt, asst_txt)
            return cls(
                fact=legacy_fact,
                fact_type=FACT_TYPE_LEGACY_PAIR,
                confidence=0.5,  # 旧数据不可信，标一个中等置信度
                ts=0.0,
                user_id="",     # 旧版未存 user_id
                conv_id=payload.get("conv_id") or "",
                source_msg_id=int(payload.get("msg_idx") or 0),
                source_user_msg=user_txt[:400],
                source_assistant_msg=asst_txt[:400],
                superseded_by=0,
                legacy=True,
            )

        return None

    # ── 渲染（供 context_builder 注入系统提示） ───────────────────────────────

    def render_for_context(self) -> str:
        """
        渲染为注入系统提示的一行字符串。
        旧数据保留 Q&A 格式，新数据直接用事实句。
        """
        if self.legacy:
            if self.source_user_msg and self.source_assistant_msg:
                return f"用户: {self.source_user_msg}\n助手: {self.source_assistant_msg}"
            return self.source_user_msg or self.source_assistant_msg or self.fact
        return self.fact


def _legacy_pair_to_fact(user: str, assistant: str) -> str:
    """把旧版 Q&A 对折叠成一个"事实"字符串（用于 legacy FactRecord.fact）。"""
    user = user[:200]
    assistant = assistant[:200]
    if user and assistant:
        return f"历史对话：用户问「{user}」，助手答「{assistant}」"
    return user or assistant or "(空记忆)"
