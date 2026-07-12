"""
数据库增量迁移：在 lifespan 中 create_all 之后执行。
所有语句幂等（IF NOT EXISTS / IF EXISTS），可重复运行。
"""
import logging

from sqlalchemy import text

logger = logging.getLogger("db.migrate")

_MIGRATIONS = [
    # ── conversations 补字段 ──
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS mode VARCHAR(20) NOT NULL DEFAULT 'agent'",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS model_name VARCHAR(100) NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS core_memory JSONB NOT NULL DEFAULT '{}'",

    # ── messages 补字段 ──
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_id VARCHAR(36) NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS thinking TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS stream_buffer TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS stream_completed BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS sequence_number INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS images JSONB NOT NULL DEFAULT '[]'",

    # ── messages 索引 ──
    "CREATE INDEX IF NOT EXISTS ix_messages_conv_seq ON messages(conv_id, sequence_number)",
    "CREATE INDEX IF NOT EXISTS ix_messages_message_id ON messages(message_id)",

    # ── tool_executions 表 ──
    """CREATE TABLE IF NOT EXISTS tool_executions (
        id              SERIAL           NOT NULL,
        conv_id         VARCHAR(36)      NOT NULL,
        message_id      VARCHAR(36)      NOT NULL DEFAULT '',
        tool_name       VARCHAR(100)     NOT NULL,
        tool_input      JSONB            NOT NULL DEFAULT '{}',
        tool_output     TEXT             NOT NULL DEFAULT '',
        search_items    JSONB            NOT NULL DEFAULT '[]',
        status          VARCHAR(20)      NOT NULL DEFAULT 'running',
        sequence_number INTEGER          NOT NULL DEFAULT 0,
        duration        DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at      DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
        CONSTRAINT pk_tool_executions PRIMARY KEY (id),
        CONSTRAINT fk_toolexec_conv FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS ix_toolexec_conv ON tool_executions(conv_id)",
    "CREATE INDEX IF NOT EXISTS ix_toolexec_msg ON tool_executions(message_id)",

    # ── event_log 表 ──
    """CREATE TABLE IF NOT EXISTS event_log (
        id              SERIAL           NOT NULL,
        conv_id         VARCHAR(36)      NOT NULL,
        message_id      VARCHAR(36)      NOT NULL DEFAULT '',
        event_type      VARCHAR(50)      NOT NULL,
        event_data      JSONB            NOT NULL DEFAULT '{}',
        sse_string      TEXT             NOT NULL DEFAULT '',
        created_at      DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
        CONSTRAINT pk_event_log PRIMARY KEY (id),
        CONSTRAINT fk_eventlog_conv FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS ix_eventlog_conv ON event_log(conv_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventlog_conv_id ON event_log(conv_id, id)",

    # ── conversations 补字段（沙箱会话持久化） ──
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS sandbox_worker_id VARCHAR(50) NOT NULL DEFAULT ''",

    # ── messages 补字段（工具/步骤摘要分离 + 澄清数据） ──
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS step_summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS clarification_data JSONB NOT NULL DEFAULT '{}'",

    # ── artifacts 补字段（message_id 关联 + size 元数据） ──
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS message_id VARCHAR(36) NOT NULL DEFAULT ''",
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS size INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS slide_count INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_message ON artifacts(message_id)",

    # ── tool_executions 补字段（步骤索引，关联计划步骤） ──
    "ALTER TABLE tool_executions ADD COLUMN IF NOT EXISTS step_index INTEGER DEFAULT NULL",

    # ── plan_steps 补字段（关联 assistant 消息） ──
    "ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS message_id VARCHAR(36) NOT NULL DEFAULT ''",

    # ── messages 补字段（结构化思考段：全披露、不覆盖） ──
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS thinking_segments JSONB NOT NULL DEFAULT '[]'",

    # ── artifacts 补字段（区分用户上传 vs 工具产出） ──
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'generated'",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_conv_source ON artifacts(conv_id, source)",

    # ── quant_snapshots 表 ──
    """CREATE TABLE IF NOT EXISTS quant_snapshots (
        id              VARCHAR(50)      NOT NULL,
        client_id       VARCHAR(36)      NOT NULL,
        conversation_id VARCHAR(36)      DEFAULT NULL,
        criteria        JSONB            NOT NULL,
        rows            JSONB            NOT NULL,
        provider_trace  JSONB            NOT NULL,
        analysis        TEXT             NOT NULL DEFAULT '',
        risk_notes      JSONB            NOT NULL DEFAULT '[]',
        status          VARCHAR(20)      NOT NULL DEFAULT 'DONE',
        created_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_quant_snapshots PRIMARY KEY (id)
    )""",
    # ── messages 补字段（token 统计） ──
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS completion_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER NOT NULL DEFAULT 0",

    # ── quant_snapshots 补字段（异步计算状态追踪） ──
    "ALTER TABLE quant_snapshots ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'DONE'",

    "CREATE INDEX IF NOT EXISTS ix_quant_snapshots_client ON quant_snapshots(client_id)",

    # ── 用户认证系统 ──
    """CREATE TABLE IF NOT EXISTS users (
        id              VARCHAR(36)      NOT NULL,
        email           VARCHAR(255)     NOT NULL UNIQUE,
        name            VARCHAR(100)     NOT NULL,
        avatar_url      VARCHAR(512)     DEFAULT '',
        bio             TEXT             DEFAULT '',
        locale          VARCHAR(10)      DEFAULT 'zh-CN',
        timezone        VARCHAR(50)      DEFAULT 'Asia/Shanghai',
        password_hash   VARCHAR(255)     DEFAULT '',
        is_active       BOOLEAN          NOT NULL DEFAULT TRUE,
        is_verified     BOOLEAN          NOT NULL DEFAULT FALSE,
        last_login_at   DOUBLE PRECISION DEFAULT 0,
        created_at      DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_users PRIMARY KEY (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_users_email ON users(email)",

    """CREATE TABLE IF NOT EXISTS oauth_accounts (
        id              SERIAL           NOT NULL,
        user_id         VARCHAR(36)      NOT NULL,
        provider        VARCHAR(20)      NOT NULL,
        provider_id     VARCHAR(255)     NOT NULL,
        provider_email  VARCHAR(255)     DEFAULT '',
        provider_name   VARCHAR(100)     DEFAULT '',
        provider_avatar VARCHAR(512)     DEFAULT '',
        access_token    TEXT             DEFAULT '',
        refresh_token   TEXT             DEFAULT '',
        token_expires_at DOUBLE PRECISION DEFAULT 0,
        raw_profile     JSONB            DEFAULT '{}',
        created_at      DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_oauth_accounts PRIMARY KEY (id),
        CONSTRAINT fk_oauth_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT uq_oauth_provider UNIQUE (provider, provider_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_oauth_user ON oauth_accounts(user_id)",

    """CREATE TABLE IF NOT EXISTS user_settings (
        id              SERIAL           NOT NULL,
        user_id         VARCHAR(36)      NOT NULL UNIQUE,
        theme           VARCHAR(20)      DEFAULT 'system',
        default_model   VARCHAR(100)     DEFAULT '',
        agent_mode_default BOOLEAN       DEFAULT TRUE,
        language        VARCHAR(10)      DEFAULT 'zh-CN',
        notifications_enabled BOOLEAN    DEFAULT TRUE,
        sidebar_collapsed BOOLEAN        DEFAULT FALSE,
        custom_settings JSONB            DEFAULT '{}',
        created_at      DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_user_settings PRIMARY KEY (id),
        CONSTRAINT fk_settings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS sessions (
        id              VARCHAR(36)      NOT NULL,
        user_id         VARCHAR(36)      NOT NULL,
        refresh_token_hash VARCHAR(255)  NOT NULL UNIQUE,
        device_info     VARCHAR(255)     DEFAULT '',
        ip_address      VARCHAR(50)      DEFAULT '',
        is_active       BOOLEAN          NOT NULL DEFAULT TRUE,
        expires_at      DOUBLE PRECISION NOT NULL,
        created_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_sessions PRIMARY KEY (id),
        CONSTRAINT fk_session_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id)",

    # 现有业务表补 user_id
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id VARCHAR(36) NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_conversations_user ON conversations(user_id)",

    "ALTER TABLE quant_snapshots ADD COLUMN IF NOT EXISTS user_id VARCHAR(36) NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_quant_snapshots_user ON quant_snapshots(user_id)",

    # ── user_usage_logs 表 ──
    """CREATE TABLE IF NOT EXISTS user_usage_logs (
        id              SERIAL           NOT NULL,
        user_id         VARCHAR(36)      NOT NULL DEFAULT '',
        client_id       VARCHAR(36)      NOT NULL DEFAULT '',
        conv_id         VARCHAR(36)      NOT NULL DEFAULT '',
        node            VARCHAR(50)      NOT NULL DEFAULT '',
        model           VARCHAR(100)     NOT NULL DEFAULT '',
        prompt_tokens   INTEGER          NOT NULL DEFAULT 0,
        completion_tokens INTEGER        NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER         NOT NULL DEFAULT 0,
        total_tokens    INTEGER          NOT NULL DEFAULT 0,
        created_at      DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_user_usage_logs PRIMARY KEY (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_usage_user_created ON user_usage_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_usage_client_created ON user_usage_logs(client_id, created_at)",

    # ── quant_snapshots 补字段（结构化思考段，spec §模型思考流程） ──
    "ALTER TABLE quant_snapshots ADD COLUMN IF NOT EXISTS thinking_segments JSONB NOT NULL DEFAULT '[]'",

    # ── prediction_snapshots 表（股票预测，仿 quant_snapshots + 预测字段） ──
    """CREATE TABLE IF NOT EXISTS prediction_snapshots (
        id                   VARCHAR(50)      NOT NULL,
        client_id            VARCHAR(36)      NOT NULL,
        user_id              VARCHAR(36)      NOT NULL DEFAULT '',
        conversation_id      VARCHAR(36)      DEFAULT NULL,
        criteria             JSONB            NOT NULL DEFAULT '{}',
        market               VARCHAR(16)      NOT NULL DEFAULT '',
        symbol               VARCHAR(32)      NOT NULL DEFAULT '',
        status               VARCHAR(24)      NOT NULL DEFAULT 'queued',
        horizon              INTEGER          NOT NULL DEFAULT 5,
        target_date          VARCHAR(16)      DEFAULT NULL,
        predicted_price      DOUBLE PRECISION DEFAULT NULL,
        predicted_range      JSONB            DEFAULT NULL,
        confidence           DOUBLE PRECISION DEFAULT NULL,
        model_name           VARCHAR(64)      NOT NULL DEFAULT '',
        news_digest          JSONB            NOT NULL DEFAULT '[]',
        news_sentiment       DOUBLE PRECISION DEFAULT NULL,
        forecast             JSONB            NOT NULL DEFAULT '[]',
        inflection_points    JSONB            NOT NULL DEFAULT '[]',
        confidence_band      JSONB            NOT NULL DEFAULT '[]',
        technical_indicators JSONB            NOT NULL DEFAULT '{}',
        analysis             TEXT             NOT NULL DEFAULT '',
        risk_notes           JSONB            NOT NULL DEFAULT '[]',
        thinking_segments    JSONB            NOT NULL DEFAULT '[]',
        error_message        TEXT             NOT NULL DEFAULT '',
        created_at           DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_prediction_snapshots PRIMARY KEY (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_prediction_snapshots_client ON prediction_snapshots(client_id)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_snapshots_user ON prediction_snapshots(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_snapshots_status ON prediction_snapshots(status)",

    # ── prediction_snapshots 补字段（阶段进度快照 + 最后更新时间，供刷新续连贯） ──
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS progress JSONB NOT NULL DEFAULT '{}'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0",

    # ── prediction_snapshots 补字段（v2：数据型维度 / 公式可解释 / 预测蜡烛 / 事件打点） ──
    # create_all 只建新表、不会 ALTER 已有表 → 这些列必须在这里显式补，且要能在有存量数据的表上重复执行。
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS filing_digest JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS model_params JSONB NOT NULL DEFAULT '{}'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS formula_steps JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS forecast_candles JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS event_markers JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS tool_traces JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS event_score DOUBLE PRECISION DEFAULT NULL",
    # 已完成记录的历史 K 线（compute 阶段落库，前端图表契约结构）→ 点开详情直接渲染，不再现拉。
    # 空对象表示存量旧记录未落库，前端回退现拉。幂等：ADD COLUMN IF NOT EXISTS 可在有数据的表上重复执行。
    "ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS historical_bars JSONB NOT NULL DEFAULT '{}'",
    # v2：默认 horizon 从 5 改为 7 个交易日（存量行不动，只改列默认值）
    "ALTER TABLE prediction_snapshots ALTER COLUMN horizon SET DEFAULT 7",

    # ── announcement_cache 表（公告 LLM 判定缓存，防止重复烧钱） ──
    """CREATE TABLE IF NOT EXISTS announcement_cache (
        id             SERIAL           NOT NULL,
        market         VARCHAR(16)      NOT NULL,
        symbol         VARCHAR(32)      NOT NULL,
        ann_date       VARCHAR(16)      NOT NULL,
        title_hash     VARCHAR(64)      NOT NULL,
        title          TEXT             NOT NULL DEFAULT '',
        ann_type       VARCHAR(64)      NOT NULL DEFAULT '',
        url            TEXT             NOT NULL DEFAULT '',
        source         VARCHAR(24)      NOT NULL DEFAULT '',
        event_type     VARCHAR(32)      NOT NULL DEFAULT '',
        negative_score DOUBLE PRECISION DEFAULT NULL,
        reason         TEXT             NOT NULL DEFAULT '',
        judged_by      VARCHAR(16)      NOT NULL DEFAULT '',
        fetched_at     DOUBLE PRECISION NOT NULL,
        CONSTRAINT pk_announcement_cache PRIMARY KEY (id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_anncache_key ON announcement_cache(market, symbol, ann_date, title_hash)",
    "CREATE INDEX IF NOT EXISTS ix_anncache_symbol ON announcement_cache(symbol, ann_date)",
    # 上面的 CREATE TABLE 在 create_all 已建过表的库里是 no-op（表已存在），
    # 补齐文本列的 DEFAULT ''，保证「create_all 建的表」与「migration 建的表」结构一致
    "ALTER TABLE announcement_cache ALTER COLUMN title SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN ann_type SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN url SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN source SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN event_type SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN reason SET DEFAULT ''",
    "ALTER TABLE announcement_cache ALTER COLUMN judged_by SET DEFAULT ''",
]


async def run_migrations(conn) -> None:
    """执行所有迁移语句（幂等）。"""
    for sql in _MIGRATIONS:
        try:
            await conn.execute(text(sql))
        except Exception as exc:
            # 跳过已存在的约束等非致命错误
            logger.debug("迁移语句跳过: %s | %s", sql[:60], exc)
    logger.info("数据库迁移完成（%d 条语句）", len(_MIGRATIONS))
