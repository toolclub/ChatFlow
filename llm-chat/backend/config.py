"""
集中管理所有配置，支持 .env 文件和环境变量覆盖。
author: leizihao

用法：
  所有配置均可通过 .env 文件或环境变量覆盖，环境变量优先级最高。
  示例：LLM_BASE_URL=http://my-server:11434/v1

复杂配置（ROUTE_MODEL_MAP / MCP_SERVERS）可在 .env 中写 JSON 字符串：
  ROUTE_MODEL_MAP={"code":"qwen3-coder:30b","search":"qwen3:8b","chat":"qwen3:8b","search_code":"qwen3-coder:30b"}
"""
import json
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 文件位于项目根目录（llm-chat/.env）或当前工作目录
_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM 服务（OpenAI 兼容接口） ──────────────────────────────────────────
    # Ollama:     http://localhost:11434/v1
    # vLLM:       http://localhost:8000/v1
    # LM Studio:  http://localhost:1234/v1
    # OpenAI:     https://api.openai.com/v1
    # Docker 中连接宿主机 Ollama: http://host.docker.internal:11434/v1
    llm_base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"

    # Embedding 单独配置（Ollama 原生 API，不走 /v1）
    ollama_base_url: str = "http://localhost:11434"

    # ── 模型配置 ─────────────────────────────────────────────────────────────
    chat_model: str = "qwen3:8b"
    summary_model: str = "qwen3:8b"
    embedding_model: str = "bge-m3"

    # ── 路由 Agent ────────────────────────────────────────────────────────────
    router_enabled: bool = True
    router_model: str = "qwen3:8b"
    search_model: str = "qwen3:8b"

    # 路由模型映射（可通过 ROUTE_MODEL_MAP='{...}' 环境变量覆盖）
    route_model_map: dict[str, str] = {
        "code":        "qwen3-coder:30b",
        "search":      "qwen3:8b",
        "chat":        "qwen3:8b",
        "search_code": "qwen3-coder:30b",
    }

    # ── 上下文窗口 ────────────────────────────────────────────────────────────
    chat_num_ctx: int = 4096
    summary_num_ctx: int = 2048
    fetch_webpage_max_display: int = 500

    # ── 记忆参数 ──────────────────────────────────────────────────────────────
    short_term_max_turns: int = 10
    compress_trigger: int = 8
    max_summary_length: int = 500
    short_term_forget_turns: int = 2

    # ── 长期记忆（Qdrant） ────────────────────────────────────────────────────
    longterm_memory_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "llm_chat_memories"
    embedding_dim: int = 1024
    longterm_top_k: int = 3
    longterm_score_threshold: float = 0.5
    summary_relevance_threshold: float = 0.4

    # ── MCP 服务器配置（可通过 MCP_SERVERS='{...}' 环境变量覆盖） ─────────────
    # 格式 stdio: {"server_name": {"command": "npx", "args": [...], "transport": "stdio"}}
    # 格式 SSE:   {"server_name": {"url": "http://...", "transport": "sse"}}
    mcp_servers: dict[str, Any] = {}

    # ── 服务端口 ──────────────────────────────────────────────────────────────
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    # ── 持久化目录 ────────────────────────────────────────────────────────────
    conversations_dir: str = "./conversations"

    # ── 数据库 ────────────────────────────────────────────────────────────────
    database_url: str = "postgresql://chatflow:chatflow123@localhost:5432/chatflow"

    # ── 日志目录 ──────────────────────────────────────────────────────────────
    log_dir: str = "./logs"

    # ── 默认系统提示词 ────────────────────────────────────────────────────────
    default_system_prompt: str = (
        "你是一个准确、诚实的AI助手，用中文回答用户问题。\n"
        "\n"
        "你拥有可以调用的工具。遇到以下情况时，必须主动调用工具获取信息，不能凭记忆猜测：\n"
        "- 需要实时或最新数据（新闻、价格、天气、版本号等）\n"
        "- 需要核实具体事实（某技术/产品的发布时间、来源公司、具体规格等）\n"
        "- 需要查阅外部资料（官方文档、代码库、参考页面等）\n"
        "- 对自己的回答没有十足把握时\n"
        "\n"
        "调用工具后，基于工具返回的真实内容作答，不要凭猜测补充工具未返回的信息。\n"
        "对于通用原理、编程概念、数学、翻译、写作等你有把握的问题，直接回答即可。"
    )
    summary_system_prompt: str = (
        "你是一个专业的摘要助手。你的任务是把对话历史压缩成简洁的摘要。"
        "要求：保留关键信息、用户偏好、重要结论和待办事项。用中文输出。"
        "不要遗漏任何重要的事实或数字。"
    )

    # ── 向后兼容：QDRANT_HOST / QDRANT_PORT（从 qdrant_url 派生） ─────────────
    @property
    def qdrant_host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.qdrant_url).hostname or "localhost"

    @property
    def qdrant_port(self) -> int:
        from urllib.parse import urlparse
        return urlparse(self.qdrant_url).port or 6333

    @property
    def api_base_url(self) -> str:
        """向后兼容别名。"""
        return self.llm_base_url


# 全局单例
settings = Settings()

# ── 向后兼容导出（所有现有 `from config import X` 无需修改） ──────────────────
LLM_BASE_URL              = settings.llm_base_url
API_KEY                   = settings.api_key
OLLAMA_BASE_URL           = settings.ollama_base_url
API_BASE_URL              = settings.api_base_url

CHAT_MODEL                = settings.chat_model
SUMMARY_MODEL             = settings.summary_model
EMBEDDING_MODEL           = settings.embedding_model

ROUTER_ENABLED            = settings.router_enabled
ROUTER_MODEL              = settings.router_model
SEARCH_MODEL              = settings.search_model
ROUTE_MODEL_MAP           = settings.route_model_map

CHAT_NUM_CTX              = settings.chat_num_ctx
SUMMARY_NUM_CTX           = settings.summary_num_ctx
FETCH_WEBPAGE_MAX_DISPLAY = settings.fetch_webpage_max_display

SHORT_TERM_MAX_TURNS      = settings.short_term_max_turns
COMPRESS_TRIGGER          = settings.compress_trigger
MAX_SUMMARY_LENGTH        = settings.max_summary_length
SHORT_TERM_FORGET_TURNS   = settings.short_term_forget_turns

LONGTERM_MEMORY_ENABLED   = settings.longterm_memory_enabled
QDRANT_URL                = settings.qdrant_url
QDRANT_HOST               = settings.qdrant_host
QDRANT_PORT               = settings.qdrant_port
QDRANT_COLLECTION         = settings.qdrant_collection
EMBEDDING_DIM             = settings.embedding_dim
LONGTERM_TOP_K            = settings.longterm_top_k
LONGTERM_SCORE_THRESHOLD  = settings.longterm_score_threshold
SUMMARY_RELEVANCE_THRESHOLD = settings.summary_relevance_threshold

MCP_SERVERS               = settings.mcp_servers

BACKEND_HOST              = settings.backend_host
BACKEND_PORT              = settings.backend_port

CONVERSATIONS_DIR         = settings.conversations_dir

DEFAULT_SYSTEM_PROMPT     = settings.default_system_prompt
SUMMARY_SYSTEM_PROMPT     = settings.summary_system_prompt

DATABASE_URL              = settings.database_url
LOG_DIR                   = settings.log_dir
