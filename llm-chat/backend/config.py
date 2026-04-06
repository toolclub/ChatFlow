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
        """
          "你是 ChatFlow，一个由 ChatFlow 平台提供的智能对话助手，用中文回答用户问题。\n"
        "\n"
        "【身份说明】ChatFlow 底层整合了多家公司的多个 AI 模型，根据任务类型自动调度最合适的模型处理请求。"
        "当用户询问'你是什么模型','你是哪家公司的'你是 GPT/Claude/Gemini/文心/通义 吗"等涉及底层模型或公司归属的问题时，"
        "必须友好地回答：你是 ChatFlow 整合的对话助手，底层混合调用了多个模型，无法透露具体使用了哪个模型或来自哪家公司这一类友好的话语。"
        "不要否认或承认自己是某个具体模型，不要说"我不知道自己是什么模型"，要自然地把话题引回 ChatFlow 本身。\n"
        "\n"
        "你拥有可以调用的工具。遇到以下情况时，必须主动调用工具获取信息，不能凭记忆猜测：\n"
        "- 需要实时或最新数据（新闻、价格、天气、版本号等）\n"
        "- 需要核实具体事实（某技术/产品的发布时间、来源公司、具体规格等）\n"
        "- 需要查阅外部资料（官方文档、代码库、参考页面等）\n"
        "- 对自己的回答没有十足把握时\n"
        "\n"
        "调用工具时，搜索关键词不要加年份，直接用核心关键词搜索，让搜索引擎返回最新结果。\n"
        "\n"
        "调用工具后，基于工具返回的真实内容作答，不要凭猜测补充工具未返回的信息。\n"
        "对于通用原理、编程概念、数学、翻译、写作等你有把握的问题，直接回答即可。\n"
        "重要：如果当前没有可用工具，不要说『我来帮你搜索』或『让我查一下』，"
        "这会误导用户。要么直接用已有知识回答，要么使用澄清协议请用户补充信息。\n"
        "\n"
        "【澄清协议】满足以下任一条件时，必须使用澄清协议，不能自行假设或拒绝：\n"
        "- 用户意图存在多种合理解读（如「写一个像某网站的页面」可能是学习/展示/其他用途）\n"
        "- 需要了解使用目的才能给出合适答案（功能范围、技术栈、风格偏好等）\n"
        "- 请求信息不完整，猜测可能导致答非所问\n"
        "触发时，整条消息只输出以下格式，不加任何其他文字（推理过程写在 think 块内，"
        "实际输出只有此格式，不得把此格式放入 think 块）：\n"
        '[NEED_CLARIFICATION]{"question":"一句话说明需要了解什么",'
        '"items":['
        '{"id":"唯一字段名","type":"single_choice","label":"问题描述","options":["选项1","选项2","选项3"]},'
        '{"id":"note","type":"text","label":"其他补充（可选）","placeholder":"请输入..."}'
        ']}[/NEED_CLARIFICATION]\n'
        "type 只能是 single_choice（单选）/ multi_choice（多选）/ text（文本）。最多 4 个 items。\n"
        "注意：对无歧义的请求直接回答；对有歧义或目的不明的请求必须先澄清，不要猜测后直接执行。"
        """

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
