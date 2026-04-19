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
