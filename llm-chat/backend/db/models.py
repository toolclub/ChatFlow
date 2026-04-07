"""
SQLAlchemy ORM 模型定义
"""
from sqlalchemy import Column, String, Text, Integer, Float, Index
from sqlalchemy.dialects.postgresql import JSONB

from db.database import Base


class ConversationModel(Base):
    """对话主表：存储每个对话的元数据和摘要信息"""
    __tablename__ = "conversations"

    id = Column(
        String(36), primary_key=True,
        comment="对话唯一标识（8位短UUID）"
    )
    title = Column(
        Text, nullable=False, default="新对话",
        comment="对话标题（首条用户消息前30字自动生成）"
    )
    system_prompt = Column(
        Text, nullable=False, default="",
        comment="自定义系统提示词"
    )
    mid_term_summary = Column(
        Text, nullable=False, default="",
        comment="中期摘要：旧消息压缩后的文本摘要"
    )
    mid_term_cursor = Column(
        Integer, nullable=False, default=0,
        comment="已完成摘要的消息游标（messages表的记录偏移）"
    )
    client_id = Column(
        String(36), nullable=False, default="",
        comment="浏览器唯一标识（由前端localStorage生成的UUID）",
        index=True,
    )
    created_at = Column(
        Float, nullable=False,
        comment="对话创建时间（Unix时间戳，浮点数）"
    )
    updated_at = Column(
        Float, nullable=False,
        comment="对话最后更新时间（Unix时间戳，浮点数）"
    )


class MessageModel(Base):
    """消息表：存储对话中的每一条消息"""
    __tablename__ = "messages"

    id = Column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键"
    )
    conv_id = Column(
        String(36), nullable=False,
        comment="所属对话ID，关联conversations.id",
        index=True,
    )
    role = Column(
        String(20), nullable=False,
        comment="消息角色：user（用户）/ assistant（AI）/ system（系统）"
    )
    content = Column(
        Text, nullable=False,
        comment="消息内容，assistant消息压缩后可能包含[old tools call]占位符"
    )
    created_at = Column(
        Float, nullable=False,
        comment="消息发送时间（Unix时间戳，浮点数）"
    )

    __table_args__ = (
        Index("ix_messages_conv_created", "conv_id", "created_at"),
    )


class PlanStepModel(Base):
    """执行计划表：记录多步骤任务的规划状态和各步骤结果"""
    __tablename__ = "plan_steps"

    id = Column(
        String(36), primary_key=True,
        comment="计划唯一ID（UUID）"
    )
    conv_id = Column(
        String(36), nullable=False,
        comment="所属对话ID，关联conversations.id",
        index=True,
    )
    goal = Column(
        Text, nullable=False, default="",
        comment="用户原始任务目标（含图片描述）"
    )
    steps = Column(
        JSONB, nullable=False, default=list,
        comment="步骤列表，每项含 {id,title,description,status,result}"
    )
    current_step = Column(
        Integer, nullable=False, default=0,
        comment="当前执行步骤索引（0-based）"
    )
    total_steps = Column(
        Integer, nullable=False, default=0,
        comment="总步骤数"
    )
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_plan_steps_conv_created", "conv_id", "created_at"),
    )


class ArtifactModel(Base):
    """文件产物表：记录沙箱中生成的用户可见文件"""
    __tablename__ = "artifacts"

    id = Column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键"
    )
    conv_id = Column(
        String(36), nullable=False,
        comment="所属对话ID",
        index=True,
    )
    name = Column(
        String(255), nullable=False,
        comment="文件名 (e.g. 'baidu_tech.html')"
    )
    path = Column(
        String(512), nullable=False,
        comment="沙箱内相对路径"
    )
    language = Column(
        String(50), nullable=False, default="text",
        comment="文件语言类型 (html/python/javascript/...)"
    )
    content = Column(
        Text, nullable=False,
        comment="文件完整内容"
    )
    created_at = Column(
        Float, nullable=False,
        comment="创建时间（Unix时间戳）"
    )

    __table_args__ = (
        Index("ix_artifacts_conv_created", "conv_id", "created_at"),
    )


class ToolEventModel(Base):
    """工具调用事件表：记录每个对话中使用的工具调用历史"""
    __tablename__ = "tool_events"

    id = Column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键"
    )
    conv_id = Column(
        String(36), nullable=False,
        comment="所属对话ID，关联conversations.id",
        index=True,
    )
    tool_name = Column(
        String(100), nullable=False,
        comment="工具名称（web_search / fetch_webpage / calculator / get_current_datetime 等）"
    )
    tool_input = Column(
        JSONB, nullable=False, default=dict,
        comment="工具调用参数（JSONB格式，如搜索关键词、URL等）"
    )
    created_at = Column(
        Float, nullable=False,
        comment="工具调用时间（Unix时间戳，浮点数）"
    )

    __table_args__ = (
        Index("ix_tool_events_conv_created", "conv_id", "created_at"),
    )
