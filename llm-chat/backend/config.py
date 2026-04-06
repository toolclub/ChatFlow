"""
集中管理所有配置，从 .env 文件或环境变量读取，无内置默认值。
author: leizihao

所有配置必须在 .env 中显式声明（参考 .env.example）。
复杂类型（ROUTE_MODEL_MAP / MCP_SERVERS）在 .env 中写 JSON 字符串：
  ROUTE_MODEL_MAP={"code":"qwen3-coder:30b","search":"qwen3:8b","chat":"qwen3:8b","search_code":"qwen3-coder:30b"}

加密环境变量：若存在 .env.enc 且有对应密钥文件，启动时自动解密并注入 os.environ。
"""
from pathlib import Path
from typing import Any

# 启动时尝试从 .env.enc 解密（存在则解密，不存在则跳过，兼容普通 .env 开发场景）
try:
    from decrypt_env import load_encrypted_env as _load_encrypted_env
    _load_encrypted_env()
except RuntimeError:
    raise
except Exception:
    pass  # 依赖未安装或模块找不到时静默跳过

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM 服务（OpenAI 兼容接口） ──────────────────────────────────────────
    llm_base_url: str
    api_key: str
    # Embedding 使用独立 URL，可与 LLM 指向不同提供商
    # 示例：本地 Ollama = http://localhost:11434/v1
    #        云端 Gitee  = https://ai.gitee.com/v1
    embedding_base_url: str
    # Embedding 独立 API Key，留空则回退到主 API_KEY
    embedding_api_key: str = ""

    # ── 模型配置 ─────────────────────────────────────────────────────────────
    chat_model: str
    summary_model: str
    embedding_model: str
    # 视觉模型：专门处理含图片的请求，可指向不同提供商（如本地 Ollama）
    # 留空则回退到 ROUTE_MODEL_MAP["chat"]
    vision_model: str = ""
    # 视觉模型的独立接口地址，默认复用 LLM_BASE_URL
    # 示例（Ollama 本地）：http://host.docker.internal:11434/v1
    vision_base_url: str = ""
    # 视觉模型的独立 API Key，默认复用 API_KEY
    vision_api_key: str = ""

    # ── 路由 Agent ────────────────────────────────────────────────────────────
    router_enabled: bool
    router_model: str
    search_model: str
    route_model_map: dict[str, str]

    # ── 上下文窗口 ────────────────────────────────────────────────────────────
    chat_num_ctx: int
    summary_num_ctx: int
    fetch_webpage_max_display: int

    # ── 记忆参数 ──────────────────────────────────────────────────────────────
    short_term_max_turns: int
    short_term_forget_turns: int
    compress_trigger: int
    max_summary_length: int

    # ── 长期记忆（Qdrant） ────────────────────────────────────────────────────
    longterm_memory_enabled: bool
    qdrant_url: str
    qdrant_collection: str
    embedding_dim: int
    longterm_top_k: int
    longterm_score_threshold: float
    summary_relevance_threshold: float

    # ── 语义缓存（Redis Search） ──────────────────────────────────────────────
    semantic_cache_enabled: bool
    redis_url: str
    semantic_cache_index: str
    semantic_cache_threshold: float
    semantic_cache_namespace_mode: str
    # search/search_code 结果的缓存过期时间（小时）；chat/code 永不过期
    semantic_cache_search_ttl_hours: int

    # ── 数据库 ────────────────────────────────────────────────────────────────
    database_url: str

    # ── MCP 服务器（可选，默认空） ────────────────────────────────────────────
    mcp_servers: dict[str, Any] = {}

    # ── 服务端口 ──────────────────────────────────────────────────────────────
    backend_host: str
    backend_port: int

    # ── 目录 ─────────────────────────────────────────────────────────────────
    conversations_dir: str
    log_dir: str

    # ── 系统提示词（长文本，保留在代码中）────────────────────────────────────
    default_system_prompt: str = (
        "你是 ChatFlow 智能助手，用中文回答用户问题。\n"
        "\n"
        "【身份】ChatFlow 混合调用多家厂商模型，不透露具体底层模型或公司归属；"
        "被问及时，自然引回‘ChatFlow 整合了多个模型’即可。\n"
        "\n"
        "【工具使用】有工具可用时，以下情况必须主动调用，不得凭记忆猜测：\n"
        "- 实时或最新信息（新闻、价格、天气、版本号、赛事结果等）\n"
        "- 你不确定或没有把握的具体事实、数据、文档内容\n"
        "- 需要了解某个你不熟悉的产品、概念、技术细节时\n"
        "工具返回后只基于实际内容作答，不补充猜测。\n"
        "无工具可用时直接回答，不要说‘帮你搜索/查一下’之类的话。\n"
        "\n"
        "【澄清协议】以下情况必须触发，不得自行假设后直接执行：\n"
        "① 用户意图存在多种解读，需要确认目的\n"
        "② 风格/范围/技术栈等关键偏好缺失，猜测会导致答非所问\n"
        "③ 用户要生成网页/落地页/UI 界面/H5，必须先确认设计风格、色调、技术方案\n"
        "触发时整条消息只输出如下格式（推理过程写 think 块内，格式本身不放入 think 块）：\n"
        '[NEED_CLARIFICATION]{"question":"一句话说明要了解什么",'
        '"items":['
        '{"id":"字段名","type":"single_choice","label":"问题","options":["A","B","C"]},'
        '{"id":"note","type":"text","label":"其他补充（可选）","placeholder":"请输入..."}'
        ']}[/NEED_CLARIFICATION]\n'
        "type 仅限 single_choice/multi_choice/text，最多 4 项。\n"
        "对明确无歧义的普通请求直接回答，不要过度触发澄清。"
    )
    summary_system_prompt: str = (
        "你是一个专业的摘要助手。你的任务是把对话历史压缩成简洁的摘要。"
        "要求：保留关键信息、用户偏好、重要结论和待办事项。用中文输出。"
        "不要遗漏任何重要的事实或数字。"
    )

    # ── 向后兼容：从 qdrant_url 派生 ─────────────────────────────────────────
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
        return self.llm_base_url


# 全局单例
settings = Settings()

# ── 向后兼容导出（所有现有 `from config import X` 无需修改） ──────────────────
LLM_BASE_URL              = settings.llm_base_url
API_KEY                   = settings.api_key
EMBEDDING_BASE_URL        = settings.embedding_base_url
# Embedding API Key：优先用独立 key，未配置时回退到主 API_KEY
EMBEDDING_API_KEY         = settings.embedding_api_key or settings.api_key
API_BASE_URL              = settings.api_base_url

CHAT_MODEL                = settings.chat_model
SUMMARY_MODEL             = settings.summary_model
EMBEDDING_MODEL           = settings.embedding_model
VISION_MODEL              = settings.vision_model
# 视觉接口：未配置时回退到主 LLM 接口
VISION_BASE_URL           = settings.vision_base_url or settings.llm_base_url
VISION_API_KEY            = settings.vision_api_key or settings.api_key

ROUTER_ENABLED            = settings.router_enabled
ROUTER_MODEL              = settings.router_model
SEARCH_MODEL              = settings.search_model
ROUTE_MODEL_MAP           = settings.route_model_map

CHAT_NUM_CTX              = settings.chat_num_ctx
SUMMARY_NUM_CTX           = settings.summary_num_ctx
FETCH_WEBPAGE_MAX_DISPLAY = settings.fetch_webpage_max_display

SHORT_TERM_MAX_TURNS      = settings.short_term_max_turns
SHORT_TERM_FORGET_TURNS   = settings.short_term_forget_turns
COMPRESS_TRIGGER          = settings.compress_trigger
MAX_SUMMARY_LENGTH        = settings.max_summary_length

LONGTERM_MEMORY_ENABLED   = settings.longterm_memory_enabled
QDRANT_URL                = settings.qdrant_url
QDRANT_HOST               = settings.qdrant_host
QDRANT_PORT               = settings.qdrant_port
QDRANT_COLLECTION         = settings.qdrant_collection
EMBEDDING_DIM             = settings.embedding_dim
LONGTERM_TOP_K            = settings.longterm_top_k
LONGTERM_SCORE_THRESHOLD  = settings.longterm_score_threshold
SUMMARY_RELEVANCE_THRESHOLD = settings.summary_relevance_threshold

SEMANTIC_CACHE_ENABLED           = settings.semantic_cache_enabled
REDIS_URL                        = settings.redis_url
SEMANTIC_CACHE_INDEX             = settings.semantic_cache_index
SEMANTIC_CACHE_THRESHOLD         = settings.semantic_cache_threshold
SEMANTIC_CACHE_NAMESPACE_MODE    = settings.semantic_cache_namespace_mode
SEMANTIC_CACHE_SEARCH_TTL_HOURS  = settings.semantic_cache_search_ttl_hours

MCP_SERVERS               = settings.mcp_servers

BACKEND_HOST              = settings.backend_host
BACKEND_PORT              = settings.backend_port

CONVERSATIONS_DIR         = settings.conversations_dir
DATABASE_URL              = settings.database_url
LOG_DIR                   = settings.log_dir

DEFAULT_SYSTEM_PROMPT     = settings.default_system_prompt
SUMMARY_SYSTEM_PROMPT     = settings.summary_system_prompt
