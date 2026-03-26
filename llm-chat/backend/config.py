"""
集中管理所有配置，方便后续调整。
author: leizihao
"""

# ── LLM 服务（OpenAI 兼容接口，支持 Ollama / vLLM / LM Studio / OpenAI 等） ──
# Ollama:     http://localhost:11434/v1
# vLLM:       http://localhost:8000/v1
# LM Studio:  http://localhost:1234/v1
# OpenAI:     https://api.openai.com/v1
LLM_BASE_URL = "http://localhost:11434/v1"
API_KEY = "ollama"          # OpenAI 填真实 key，本地服务随便填非空字符串即可

# Embedding 单独配置（使用 Ollama 原生 API，不走 /v1）
# 如果你的 Embedding 模型也在远程服务，改这里即可
OLLAMA_BASE_URL = "http://localhost:11434"

# 向后兼容
API_BASE_URL = LLM_BASE_URL

# ── 模型配置 ──
CHAT_MODEL = "mistral-nemo"          # 默认对话模型（前端未指定时使用）
SUMMARY_MODEL = "qwen3:8b"
EMBEDDING_MODEL = "bge-m3"

# ── 路由 Agent 配置 ──
ROUTER_ENABLED = True                # False 时跳过路由，直接用前端选择的模型
ROUTER_MODEL = "qwen3:8b"           # 用于意图分类的小模型（快）

# 工具调用专用模型（必须支持 function calling）
# search / search_code 路由的第一阶段（tool_model）都用这个模型做工具调用
SEARCH_MODEL = "qwen3:8b"

# 最终回答模型映射（answer_model）
ROUTE_MODEL_MAP = {
    "code":        "qwen3-coder:30b",  # 纯代码问题，直接回答，不调工具
    "search":      "qwen3:8b",         # 搜索后回答
    "chat":        "qwen3:8b",         # 普通对话
    "search_code": "qwen3-coder:30b",  # 先用 SEARCH_MODEL 搜索，再用代码模型写代码
}

# fetch_webpage 工具的 SSE 事件也走结构化（在 runner.py 里处理）
FETCH_WEBPAGE_MAX_DISPLAY = 500  # 前端展示的页面内容最大字符数

# ── 上下文窗口 ──
CHAT_NUM_CTX = 4096
SUMMARY_NUM_CTX = 2048

# ── 记忆参数 ──
SHORT_TERM_MAX_TURNS = 10
COMPRESS_TRIGGER = 8
MAX_SUMMARY_LENGTH = 500
SHORT_TERM_FORGET_TURNS = 2

# ── 长期记忆（Qdrant 向量库） ──
# 设为 False 可在未部署 Qdrant 的环境下完全跳过长期记忆，不影响其他功能
LONGTERM_MEMORY_ENABLED = True
QDRANT_URL = "http://localhost:6333"
QDRANT_HOST = "localhost"         # 向后兼容
QDRANT_PORT = 6333                # 向后兼容
QDRANT_COLLECTION = "llm_chat_memories"
EMBEDDING_DIM = 1024
LONGTERM_TOP_K = 3
LONGTERM_SCORE_THRESHOLD = 0.5
SUMMARY_RELEVANCE_THRESHOLD = 0.4

# ── MCP 服务器配置 ──
# 格式 stdio: {"server_name": {"command": "npx", "args": [...], "transport": "stdio"}}
# 格式 SSE:   {"server_name": {"url": "http://...", "transport": "sse"}}
# 留空则不加载任何 MCP 工具
MCP_SERVERS: dict = {
    # 示例（取消注释以启用文件系统 MCP 服务器）：
    # "filesystem": {
    #     "command": "npx",
    #     "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data"],
    #     "transport": "stdio",
    # },
    # 示例（SSE 传输）：
    # "my_sse_server": {
    #     "url": "http://localhost:8080/sse",
    #     "transport": "sse",
    # },
}

# ── 服务端口 ──
BACKEND_HOST = "0.0.0.0"
BACKEND_PORT = 8000

# ── 持久化目录 ──
CONVERSATIONS_DIR = "./conversations"

# ── 默认系统提示词 ──
DEFAULT_SYSTEM_PROMPT = (
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
SUMMARY_SYSTEM_PROMPT = (
    "你是一个专业的摘要助手。你的任务是把对话历史压缩成简洁的摘要。"
    "要求：保留关键信息、用户偏好、重要结论和待办事项。用中文输出。"
    "不要遗漏任何重要的事实或数字。"
)
