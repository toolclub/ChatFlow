"""
SQLAlchemy ORM 模型定义（重构版 — DB-first 架构）

核心变更：
  - messages 表新增 thinking/stream_buffer/stream_completed/message_id/sequence_number
  - 新增 event_log 表：SSE 事件持久化（替代纯内存 event_buffer）
  - 新增 tool_executions 表：工具调用独立记录
  - 保留 message_details 表向后兼容（逐步废弃）
"""
from sqlalchemy import Column, String, Text, Integer, Float, Boolean, Index, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from db.database import Base


class UserModel(Base):
    """用户主表"""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    avatar_url = Column(String(512), default="")
    bio = Column(Text, default="")
    locale = Column(String(10), default="zh-CN")
    timezone = Column(String(50), default="Asia/Shanghai")
    password_hash = Column(String(255), default="")
    is_active = Column(Boolean, nullable=False, default=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    last_login_at = Column(Float, default=0)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    # 关联关系
    oauth_accounts = relationship("OAuthAccountModel", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSettingsModel", back_populates="user", uselist=False, cascade="all, delete-orphan")
    sessions = relationship("SessionModel", back_populates="user", cascade="all, delete-orphan")


class OAuthAccountModel(Base):
    """OAuth 账号关联表"""
    __tablename__ = "oauth_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(20), nullable=False)  # google, github
    provider_id = Column(String(255), nullable=False)
    provider_email = Column(String(255), default="")
    provider_name = Column(String(100), default="")
    provider_avatar = Column(String(512), default="")
    access_token = Column(Text, default="")
    refresh_token = Column(Text, default="")
    token_expires_at = Column(Float, default=0)
    raw_profile = Column(JSONB, default=dict)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    user = relationship("UserModel", back_populates="oauth_accounts")

    __table_args__ = (
        Index("uq_oauth_provider", "provider", "provider_id", unique=True),
    )


class UserSettingsModel(Base):
    """用户设置表"""
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    theme = Column(String(20), default="system")
    default_model = Column(String(100), default="")
    agent_mode_default = Column(Boolean, default=True)
    language = Column(String(10), default="zh-CN")
    notifications_enabled = Column(Boolean, default=True)
    sidebar_collapsed = Column(Boolean, default=False)
    custom_settings = Column(JSONB, default=dict)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    user = relationship("UserModel", back_populates="settings")


class SessionModel(Base):
    """登录会话表 (Refresh Token)"""
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash = Column(String(255), nullable=False, unique=True, index=True)
    device_info = Column(String(255), default="")
    ip_address = Column(String(50), default="")
    is_active = Column(Boolean, nullable=False, default=True)
    expires_at = Column(Float, nullable=False)
    created_at = Column(Float, nullable=False)

    user = relationship("UserModel", back_populates="sessions")


class ConversationModel(Base):
    """对话主表"""
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True)
    title = Column(Text, nullable=False, default="新对话")
    system_prompt = Column(Text, nullable=False, default="")
    core_memory = Column(
        JSONB, nullable=False, default=dict,
        comment="核心记忆块：用户偏好/项目规则/当前任务/学习到的长期偏好",
    )
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
    user_id = Column(String(36), nullable=False, default="", index=True)
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
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)
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


class QuantSnapshotModel(Base):
    """量化分析快照表"""
    __tablename__ = "quant_snapshots"

    id = Column(String(50), primary_key=True)
    client_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, default="", index=True)
    conversation_id = Column(String(36), nullable=True)
    criteria = Column(JSONB, nullable=False)
    rows = Column(JSONB, nullable=False)
    provider_trace = Column(JSONB, nullable=False)
    analysis = Column(Text, nullable=False, default="")
    risk_notes = Column(JSONB, nullable=False, default=list)
    thinking_segments = Column(JSONB, nullable=False, default=list)
    status = Column(String(20), nullable=False, default="DONE")
    created_at = Column(Float, nullable=False)


class PredictionSnapshotModel(Base):
    """股票预测快照表（仿 QuantSnapshotModel + 预测专用字段）

    铁律 #1：status 从 DB 字段读，不推断文本；
    铁律 #6：status 存 fsm/prediction.py PredictionStatus 枚举值（真 (str, Enum)，非字面量）。
    """
    __tablename__ = "prediction_snapshots"

    id = Column(String(50), primary_key=True)
    client_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, default="", index=True)
    conversation_id = Column(String(36), nullable=True)
    criteria = Column(JSONB, nullable=False)                   # {symbol, market, methods, horizon, dimensions}
    market = Column(String(16), nullable=False, default="")    # cn_a | us_stock
    symbol = Column(String(32), nullable=False, default="")
    status = Column(String(24), nullable=False, default="queued")
    horizon = Column(Integer, nullable=False, default=7)        # 预测交易日数（v2 默认 7 个交易日）
    target_date = Column(String(16), nullable=True)             # 预测目标日 YYYY-MM-DD
    predicted_price = Column(Float, nullable=True)              # 终点价格点预测
    predicted_range = Column(JSONB, nullable=True)             # [low, high] 80% 置信
    confidence = Column(Float, nullable=True)                   # 0..1
    model_name = Column(String(64), nullable=False, default="")
    news_digest = Column(JSONB, nullable=False, default=list)   # 12 维资讯（含完整原文，不截取）
    news_sentiment = Column(Float, nullable=True)              # -1..1 综合（蒙特卡洛漂移修正因子）
    forecast = Column(JSONB, nullable=False, default=list)     # 点预测序列
    inflection_points = Column(JSONB, nullable=False, default=list)
    confidence_band = Column(JSONB, nullable=False, default=list)  # {dates, p10, p50, p90}
    technical_indicators = Column(JSONB, nullable=False, default=dict)
    analysis = Column(Text, nullable=False, default="")
    risk_notes = Column(JSONB, nullable=False, default=list)
    thinking_segments = Column(JSONB, nullable=False, default=list)
    error_message = Column(Text, nullable=False, default="")
    # 阶段进度快照（前后端共用结构）：
    # {"phase": "preflight"|"news_searching"|"inferring", "label": "地缘政治",
    #  "index": 3, "total": 12, "detail": "正在搜索…", "updated_at": 1752}
    progress = Column(JSONB, nullable=False, default=dict)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False, default=0.0)   # 最后一次进度/状态写入时间戳

    # ── v2 新增（铁律 #2：结构化产物一律独立列，不塞 content/analysis 文本） ──
    # 数据型维度结果（公告 / 定增 / 减持 / SEC filing），与 news_digest（搜索型）平级：
    # [{dimension, label, kind:"data", source, confidence, items:[...], score, failed, error}]
    filing_digest = Column(JSONB, nullable=False, default=list)
    # 计算引擎全参数（ARIMA/GARCH/事件分/GBM/蒙特卡洛/影线标定），供前端公式卡片复现推导
    model_params = Column(JSONB, nullable=False, default=dict)
    # FORMULA_STEP 事件序列（刷新后重建公式面板）：
    # [{stage, title, formula_text, formula_latex, params, substituted, result, explain}]
    formula_steps = Column(JSONB, nullable=False, default=list)
    # 预测 OHLC 蜡烛：[{day, date, o, h, l, c, p10, p50, p90}]
    forecast_candles = Column(JSONB, nullable=False, default=list)
    # K 线上的公告事件打点：[{date, event_type, title, score}]
    event_markers = Column(JSONB, nullable=False, default=list)
    # 工具调用轨迹（TOOL_TRACE）：[{tool, args, status, count, duration, error, ts}]
    tool_traces = Column(JSONB, nullable=False, default=list)
    # 事件综合分 F ∈ [-1, 0]（爆雷/定增/减持 加权饱和分），为空表示未跑数据型维度
    event_score = Column(Float, nullable=True)
    # 历史 K 线（compute 阶段落库，前端图表契约结构）：
    # {dates:[...], values:[[open,close,low,high],...], ma5/ma10/ma20:[...], volumes:[[i,vol,flag],...]}
    # 空 dict 表示旧记录未落库 → 前端回退现拉 fetchStockChart（保持兼容）
    historical_bars = Column(JSONB, nullable=False, default=dict)


class AnnouncementCacheModel(Base):
    """公告 LLM 判定缓存 —— 同一条公告（market+symbol+ann_date+title_hash）只打分一次。

    规则命中（judged_by="rule"）的结果永不过期（公告是不可变历史事实）；
    LLM 判定（judged_by="llm"）的可按 PREDICTION_ANN_CACHE_DAYS 过期后重判（模型升级重打分）。
    """
    __tablename__ = "announcement_cache"

    # server_default：本表由 create_all 建（它先于 migrate 执行），要让新库建出的表
    # 和 migrate.py 里的 DDL 完全一致（含 DEFAULT ''），否则两种部署路径会长出不同的表结构。
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(16), nullable=False)
    symbol = Column(String(32), nullable=False)
    ann_date = Column(String(16), nullable=False)
    title_hash = Column(String(64), nullable=False, comment="md5(标题)，<em> 高亮标签先剥离")
    title = Column(Text, nullable=False, default="", server_default="")
    ann_type = Column(String(64), nullable=False, default="", server_default="",
                      comment="东财「公告类型」原值")
    url = Column(Text, nullable=False, default="", server_default="")
    source = Column(String(24), nullable=False, default="", server_default="",
                    comment="eastmoney | cninfo | sec")
    event_type = Column(
        String(32), nullable=False, default="", server_default="",
        comment="negative | private_placement | stake_reduction | st_delisting_risk | violation | neutral",
    )
    negative_score = Column(Float, nullable=True, comment="-1..0；规则命中的直接写死")
    reason = Column(Text, nullable=False, default="", server_default="")
    judged_by = Column(String(16), nullable=False, default="", server_default="",
                       comment="rule | llm")
    fetched_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ux_anncache_key", "market", "symbol", "ann_date", "title_hash", unique=True),
        Index("ix_anncache_symbol", "symbol", "ann_date"),
    )


class LLMProviderModel(Base):
    """LLM 供应商配置 —— 管理页维护，管理员激活一条后全局生效（cc-switch 模式）。

    无激活行（或激活行 enabled=False）时，运行时回退 .env 默认配置。
    is_active 至多一条 True：由 db/llm_provider_store.activate_provider 在单事务内保证，
    不靠 DB 约束（部分激活 UNIQUE 索引在 PostgreSQL 无法直接表达）。
    """
    __tablename__ = "llm_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, comment="显示名（唯一）")
    base_url = Column(String(500), nullable=False, comment="OpenAI 兼容接口地址")
    api_key = Column(Text, nullable=False)
    chat_model = Column(String(200), nullable=False, comment="主对话模型")
    # 以下角色模型留空 = 沿用简单回退（见 services/llm_provider_service.resolve_chat）
    summary_model = Column(String(200), nullable=True, comment="摘要用模型，空=同 chat_model")
    router_model = Column(
        String(200), nullable=True,
        comment="路由/搜索（ROUTER/SEARCH/ROUTE_MODEL_MAP）用模型，空=同 chat_model",
    )
    vision_model = Column(String(200), nullable=True, comment="视觉模型，空=沿用 .env VISION_*（视觉不随供应商切换）")
    # 用户在聊天界面可额外选择的模型名列表（GET /api/models 返回 chat_model + extra_models）
    extra_models = Column(JSONB, nullable=False, default=list)
    remark = Column(String(500), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, comment="停用后不可被激活生效")
    is_active = Column(Boolean, nullable=False, default=False, comment="全局生效中的配置（至多一条 True）")
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ux_llmprov_name", "name", unique=True),
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


class UserUsageLogModel(Base):
    """用户/游客 Token 使用统计日志表"""
    __tablename__ = "user_usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), nullable=False, default="", index=True, comment="用户ID，为空表示游客")
    client_id = Column(String(36), nullable=False, default="", index=True, comment="浏览器唯一标识")
    conv_id = Column(String(36), nullable=False, default="", index=True)
    node = Column(String(50), nullable=False, default="", comment="产出 Token 的图节点名")
    model = Column(String(100), nullable=False, default="", comment="使用的模型名")
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_client_created", "client_id", "created_at"),
    )
