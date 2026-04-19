"""
SQLAlchemy ORM 模型定义（重构版 — DB-first 架构）

核心变更：
  - messages 表新增 thinking/stream_buffer/stream_completed/message_id/sequence_number
  - 新增 event_log 表：SSE 事件持久化（替代纯内存 event_buffer）
  - 新增 tool_executions 表：工具调用独立记录
  - 保留 message_details 表向后兼容（逐步废弃）
"""
from sqlalchemy import Column, String, Text, Integer, Float, Boolean, Index
from sqlalchemy.dialects.postgresql import JSONB

from db.database import Base


class ConversationModel(Base):
    """对话主表"""
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True)
    title = Column(Text, nullable=False, default="新对话")
    system_prompt = Column(Text, nullable=False, default="")
    mid_term_summary = Column(Text, nullable=False, default="")
    mid_term_cursor = Column(Integer, nullable=False, default=0)
    client_id = Column(String(36), nullable=False, default="", index=True)
    status = Column(
        String(20), nullable=False, default="active",
        comment="active / streaming / completed / error",
    )
    mode = Column(
        String(20), nullable=False, default="agent",
        comment="agent / chat — 用户选择的模式",
    )
    model_name = Column(
        String(100), nullable=False, default="",
        comment="使用的模型名",
    )
    sandbox_worker_id = Column(
        String(50), nullable=False, default="",
        comment="沙箱 Worker ID（持久化会话亲和性，跨 worker 恢复）",
    )
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)


class MessageModel(Base):
    """消息表（重构版 — 每条消息的全部 IO 都在此表）"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(36), nullable=False, index=True)
    message_id = Column(
        String(36), nullable=False, default="",
        comment="业务唯一 ID（UUID），用于前端关联和幂等",
    )
    role = Column(String(20), nullable=False, comment="user / assistant / system / tool")
    content = Column(Text, nullable=False, default="", comment="最终完整内容")
    thinking = Column(Text, nullable=False, default="", comment="推理过程纯文本（所有 segments 拼接，向后兼容）")
    thinking_segments = Column(
        JSONB, nullable=False, default=list,
        comment="结构化思考段 [{node, step_index, phase, content}]，按接收顺序追加",
    )
    stream_buffer = Column(
        Text, nullable=False, default="",
        comment="流式输出中间缓冲（未完成时暂存，完成后清空并写入 content）",
    )
    stream_completed = Column(
        Boolean, nullable=False, default=True,
        comment="流式输出是否完成（false=正在生成中）",
    )
    sequence_number = Column(
        Integer, nullable=False, default=0,
        comment="消息在对话中的顺序号（严格递增）",
    )
    images = Column(JSONB, nullable=False, default=list, comment="用户上传的图片（base64）")
    tool_summary = Column(
        Text, nullable=False, default="",
        comment="工具调用记录摘要（独立字段，不混入 content）",
    )
    step_summary = Column(
        Text, nullable=False, default="",
        comment="多步执行过程摘要（独立字段，不混入 content）",
    )
    clarification_data = Column(
        JSONB, nullable=False, default=dict,
        comment="澄清问询数据（question + items），非空表示本轮需要用户澄清",
    )
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_messages_conv_created", "conv_id", "created_at"),
        Index("ix_messages_conv_seq", "conv_id", "sequence_number"),
        Index("ix_messages_message_id", "message_id"),
    )


class ToolExecutionModel(Base):
    """工具调用记录表 — 每次工具调用独立记录"""
    __tablename__ = "tool_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(36), nullable=False)
    message_id = Column(String(36), nullable=False, comment="关联的 assistant 消息 ID")
    tool_name = Column(String(100), nullable=False)
    tool_input = Column(JSONB, nullable=False, default=dict)
    tool_output = Column(Text, nullable=False, default="")
    search_items = Column(JSONB, nullable=False, default=list, comment="搜索结果列表")
    status = Column(
        String(20), nullable=False, default="running",
        comment="running / done / error",
    )
    step_index = Column(
        Integer, nullable=True, default=None,
        comment="所属计划步骤索引（0-based），无计划时为 NULL",
    )
    sequence_number = Column(Integer, nullable=False, default=0)
    duration = Column(Float, nullable=False, default=0)
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_toolexec_conv", "conv_id"),
        Index("ix_toolexec_msg", "message_id"),
    )


class EventLogModel(Base):
    """事件流持久化表 — 替代纯内存 event_buffer，跨 worker 持久化"""
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID，天然有序")
    conv_id = Column(String(36), nullable=False)
    message_id = Column(String(36), nullable=False, default="")
    event_type = Column(
        String(50), nullable=False,
        comment="content_delta / thinking_delta / tool_call_start / tool_call_end / "
                "search_item / sandbox_output / plan_generated / status / route / "
                "reflection / file_artifact / done / stopped / error / ping / resume_context",
    )
    event_data = Column(JSONB, nullable=False, default=dict, comment="事件 payload")
    sse_string = Column(Text, nullable=False, default="", comment="原始 SSE 字符串（直接推给前端）")
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_eventlog_conv", "conv_id"),
        Index("ix_eventlog_conv_id", "conv_id", "id"),
    )


# ── 以下为保留表（向后兼容） ──────────────────────────────────────────────────

class PlanStepModel(Base):
    """执行计划表"""
    __tablename__ = "plan_steps"

    id = Column(String(36), primary_key=True)
    conv_id = Column(String(36), nullable=False, index=True)
    message_id = Column(
        String(36), nullable=False, default="",
        comment="关联的 assistant 消息 ID（哪条消息触发了这个计划）",
    )
    goal = Column(Text, nullable=False, default="")
    steps = Column(JSONB, nullable=False, default=list)
    current_step = Column(Integer, nullable=False, default=0)
    total_steps = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_plan_steps_conv_created", "conv_id", "created_at"),
    )


class ArtifactModel(Base):
    """文件产物表 — 关联到 message，内容按需加载"""
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(36), nullable=False, index=True)
    message_id = Column(String(36), nullable=False, default="", comment="关联的消息 ID（generated→assistant，uploaded→user）")
    name = Column(String(255), nullable=False)
    path = Column(String(512), nullable=False)
    language = Column(String(50), nullable=False, default="text")
    content = Column(Text, nullable=False)
    size = Column(Integer, nullable=False, default=0, comment="原始文件大小(bytes)")
    slide_count = Column(Integer, nullable=False, default=0, comment="PPT 页数")
    # source：'generated'=沙箱工具产出（默认，向后兼容）  'uploaded'=用户从前端拖入
    # 为什么不分表：所有读路径（meta_list/download/full-state）已按 conv_id+message_id 索引，
    # 加一列就能复用，避免 N+1 查询和前端两套渲染
    source = Column(String(16), nullable=False, default="generated", comment="generated|uploaded")
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_artifacts_conv_created", "conv_id", "created_at"),
        Index("ix_artifacts_message", "message_id"),
    )


class ToolEventModel(Base):
    """工具调用事件表（旧版，保留向后兼容）"""
    __tablename__ = "tool_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(36), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False)
    tool_input = Column(JSONB, nullable=False, default=dict)
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_tool_events_conv_created", "conv_id", "created_at"),
    )


class MessageDetailModel(Base):
    """消息详情表（旧版，保留向后兼容，逐步由 messages 新字段替代）"""
    __tablename__ = "message_details"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(36), nullable=False)
    msg_index = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False, default="")
    thinking = Column(Text, nullable=False, default="")
    tool_calls = Column(JSONB, nullable=False, default=list)
    steps = Column(JSONB, nullable=False, default=list)
    search_results = Column(JSONB, nullable=False, default=list)
    sandbox_output = Column(Text, nullable=False, default="")
    stream_completed = Column(Boolean, nullable=False, default=True)
    stream_buffer = Column(Text, nullable=False, default="")
    images = Column(JSONB, nullable=False, default=list)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_msgdetail_conv", "conv_id"),
        Index("ix_msgdetail_conv_idx", "conv_id", "msg_index"),
    )
