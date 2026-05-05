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
    # Embedding 兜底模型列表（主模型失败时依次尝试，全 JSON 数组字符串格式）
    # 示例：EMBEDDING_FALLBACK_MODELS=["Qwen3-Embedding-8B","Qwen3-Embedding-0.6B"]
    embedding_fallback_models: list[str] = []
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

    # ── 事实级长期记忆（Mem0 风格，独立开关，默认跟随 longterm_memory_enabled） ──
    # fact_extraction_enabled=false 时保留旧的批量 Q&A 写入逻辑（COMPAT）
    fact_extraction_enabled: bool = True
    fact_min_confidence: float = 0.7          # 抽取置信度过滤阈值
    fact_max_per_turn: int = 6                # 单轮最多抽取条数
    fact_update_sim_threshold: float = 0.80   # ≥ 此分数才交给 LLM 判 UPDATE/DELETE/NONE
    fact_update_candidate_top_k: int = 3      # 召回多少条候选给 updater 仲裁

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

    # ── 文生图（PPT 配图用，可选） ────────────────────────────────────────────
    image_gen_enabled: bool = False
    image_gen_base_url: str = ""     # OpenAI 兼容 images/generations 接口
    image_gen_api_key: str = ""
    image_gen_model: str = ""        # e.g. "dall-e-3", "stable-diffusion-xl"

    # ── 沙箱代码执行（可选） ──────────────────────────────────────────────────
    sandbox_enabled: bool = False
    # JSON 数组：[{"id":"w1","host":"192.168.1.100","port":22,"user":"sandbox","key_file":"~/.ssh/id_rsa"}]
    sandbox_workers: list[dict[str, Any]] = []
    sandbox_timeout: int = 120       # 单次执行超时（秒），mvn/gradle 构建需要 60~120s
    sandbox_cleanup_hours: int = 12  # session 目录过期清理（小时）

    # ── 文件上传 ─────────────────────────────────────────────────────────────
    upload_max_file_size: int = 50 * 1024 * 1024   # 单文件 50MB
    upload_max_files_per_message: int = 10         # 单条消息最多带几个文件

    # ── 量化模块（A 股选股） ─────────────────────────────────────────────────
    quant_enabled: bool = False
    # JSON 数组：["baostock","tushare","akshare"]，按顺序作为优先级
    quant_provider_order: list[str] = ["baostock", "akshare"]
    quant_bars_concurrency: int = 8        # 拉取日线时的并发上限
    quant_default_top_n: int = 30          # /api/quant/screen 默认 top_n
    quant_first_pass_keep: int = 200       # 硬过滤后保留的候选数量上限
    quant_http_timeout: int = 20

    # ── Tushare（可选，需 token + 积分） ─────────────────────────────────────
    tushare_token: str = ""

    # ── BaoStock（可选，免 token） ───────────────────────────────────────────
    baostock_enabled: bool = True

    # ── 量化磁盘缓存 / 后台预热 ──────────────────────────────────────────────
    quant_cache_dir: str = "./.quant_cache"
    quant_cache_retention_days: int = 90       # bars 保留天数
    quant_cache_max_size_mb: int = 200         # 整个 cache 目录硬上限
    quant_warmer_enabled: bool = True
    quant_warmer_spot_interval: int = 300      # 行情时间窗内 spot 刷新间隔（秒）
    quant_warmer_bars_hour: int = 16           # 每天此小时（本地）跑当天 bars
    quant_warmer_index_hour: int = 7
    quant_spot_fresh_seconds: int = 600        # spot 多久内算新鲜，过期才回源
    quant_bars_lookback_days: int = 120        # bars 默认预热区间

    # ── 服务端口 ──────────────────────────────────────────────────────────────
    backend_host: str
    backend_port: int

    # ── 目录 ─────────────────────────────────────────────────────────────────
    conversations_dir: str
    log_dir: str

    # ── JWT 配置 ──────────────────────────────────────────────────────────────
    jwt_secret_key: str = "your-jwt-secret-key-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 7
    admin_secret_key: str = ""

    # ── OAuth 配置 ─────────────────────────────────────────────────────────────
    oauth_proxy: str = ""  # OAuth 请求专用代理（如 http://host.docker.internal:7890）
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_google_redirect_uri: str = ""
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_github_redirect_uri: str = ""

    # ── Cookie 配置 ────────────────────────────────────────────────────────────
    cookie_secure: bool = False

    @property
    def cookie_domain(self) -> str | None:
        """从 frontend_url 提取父域名，使 Cookie 在 www/non-www 子域间共享。
        localhost 时返回 None（不设 domain，使用默认行为）。"""
        from urllib.parse import urlparse
        hostname = urlparse(self.frontend_url).hostname
        if not hostname or hostname in ("localhost", "127.0.0.1"):
            return None
        parts = hostname.split(".")
        if len(parts) >= 2:
            return "." + ".".join(parts[-2:])  # .chatflow-live.com
        return None

    # ── CORS 配置 ──────────────────────────────────────────────────────────────
    cors_allowed_origins: list[str] = ["http://localhost:5173", "http://localhost"]
    frontend_url: str = "http://localhost"

    # ── 系统提示词（长文本，保留在代码中）────────────────────────────────────
    # 系统提示词从 prompts/*.md 加载，这里保留空默认值供 Pydantic 验证
    # 实际值在模块底部通过 _load_prompts_into_settings() 注入
    default_system_prompt: str = ""
    summary_system_prompt: str = ""

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
EMBEDDING_FALLBACK_MODELS = settings.embedding_fallback_models
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

FACT_EXTRACTION_ENABLED       = settings.fact_extraction_enabled
FACT_MIN_CONFIDENCE           = settings.fact_min_confidence
FACT_MAX_PER_TURN             = settings.fact_max_per_turn
FACT_UPDATE_SIM_THRESHOLD     = settings.fact_update_sim_threshold
FACT_UPDATE_CANDIDATE_TOP_K   = settings.fact_update_candidate_top_k

SEMANTIC_CACHE_ENABLED           = settings.semantic_cache_enabled
REDIS_URL                        = settings.redis_url
SEMANTIC_CACHE_INDEX             = settings.semantic_cache_index
SEMANTIC_CACHE_THRESHOLD         = settings.semantic_cache_threshold
SEMANTIC_CACHE_NAMESPACE_MODE    = settings.semantic_cache_namespace_mode
SEMANTIC_CACHE_SEARCH_TTL_HOURS  = settings.semantic_cache_search_ttl_hours

MCP_SERVERS               = settings.mcp_servers

IMAGE_GEN_ENABLED         = settings.image_gen_enabled
IMAGE_GEN_BASE_URL        = settings.image_gen_base_url or settings.llm_base_url
IMAGE_GEN_API_KEY         = settings.image_gen_api_key or settings.api_key
IMAGE_GEN_MODEL           = settings.image_gen_model

SANDBOX_ENABLED           = settings.sandbox_enabled
SANDBOX_WORKERS           = settings.sandbox_workers
SANDBOX_TIMEOUT           = settings.sandbox_timeout
SANDBOX_CLEANUP_HOURS     = settings.sandbox_cleanup_hours

UPLOAD_MAX_FILE_SIZE          = settings.upload_max_file_size
UPLOAD_MAX_FILES_PER_MESSAGE  = settings.upload_max_files_per_message

BACKEND_HOST              = settings.backend_host
BACKEND_PORT              = settings.backend_port

CONVERSATIONS_DIR         = settings.conversations_dir
DATABASE_URL              = settings.database_url
LOG_DIR                   = settings.log_dir
ADMIN_SECRET_KEY          = settings.admin_secret_key

# ── JWT 配置 ──────────────────────────────────────────────────────────────
JWT_SECRET_KEY            = settings.jwt_secret_key
JWT_ALGORITHM             = settings.jwt_algorithm
JWT_ACCESS_EXPIRE_MINUTES = settings.jwt_access_expire_minutes
JWT_REFRESH_EXPIRE_DAYS   = settings.jwt_refresh_expire_days

# ── OAuth 配置 ──────────────────────────────────────────────────────────────
OAUTH_PROXY               = settings.oauth_proxy
OAUTH_GOOGLE_CLIENT_ID    = settings.oauth_google_client_id
OAUTH_GOOGLE_CLIENT_SECRET = settings.oauth_google_client_secret
OAUTH_GOOGLE_REDIRECT_URI = settings.oauth_google_redirect_uri
OAUTH_GITHUB_CLIENT_ID    = settings.oauth_github_client_id
OAUTH_GITHUB_CLIENT_SECRET = settings.oauth_github_client_secret
OAUTH_GITHUB_REDIRECT_URI = settings.oauth_github_redirect_uri

# ── Cookie/CORS 配置 ────────────────────────────────────────────────────────
COOKIE_SECURE             = settings.cookie_secure
COOKIE_DOMAIN             = settings.cookie_domain
CORS_ALLOWED_ORIGINS      = settings.cors_allowed_origins
FRONTEND_URL              = settings.frontend_url

# Load system prompts from prompts/*.md
from prompts import load_prompt as _lp
DEFAULT_SYSTEM_PROMPT     = _lp("system")
SUMMARY_SYSTEM_PROMPT     = _lp("summary")

