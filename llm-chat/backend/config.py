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

# 向后兼容
API_BASE_URL = LLM_BASE_URL

# ── 模型配置 ──
CHAT_MODEL = "mistral-nemo"          # 默认对话模型（前端未指定时使用）
SUMMARY_MODEL = "qwen3:8b"
EMBEDDING_MODEL = "bge-m3"

# ── 路由 Agent 配置 ──
ROUTER_ENABLED = True                # False 时跳过路由，直接用前端选择的模型
ROUTER_MODEL = "qwen3:8b"         # 用于意图分类的小模型（快）
ROUTE_MODEL_MAP = {
    "code":   "qwen3-coder:30b",  # 编程/代码问题
    "search": "qwen3:8b",     # 需要工具调用/搜索
    "chat":   "qwen3:8b",         # 普通对话（小模型直接回）
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
    "你是一个有用的AI助手，请用中文回答用户的问题。回答要准确、清晰、有条理。\n"
    "\n"
    "【重要】遇到以下情况，必须先用工具搜索，不能凭记忆直接回答：\n"
    "- 实时/最新信息：新闻、股价、天气、最新版本、近期事件\n"
    "- 具体事实核查：某技术/产品/概念是哪年出现的、哪个公司/人提出/发布的、\n"
    "  某事件的具体时间地点、某产品的具体参数规格\n"
    "- 近几年出现的新技术、新协议、新框架（你的训练数据可能过时或缺失）\n"
    "- 任何你没有十足把握的专有名词、小众知识\n"
    "\n"
    "不需要搜索的情况：通用原理解释、编程、数学、翻译、写作、逻辑推理、日常闲聊。\n"
    "\n"
    "搜索流程（需要搜索时严格执行）：\n"
    "1. 用 web_search 搜索，搜索词用中文\n"
    "2. 用 fetch_webpage 读取 1-3 个最相关页面的详细内容\n"
    "3. 综合多个来源给出准确回答，注明信息来源\n"
    "宁可多搜一次，也不要凭猜测给出错误答案。"
)
SUMMARY_SYSTEM_PROMPT = (
    "你是一个专业的摘要助手。你的任务是把对话历史压缩成简洁的摘要。"
    "要求：保留关键信息、用户偏好、重要结论和待办事项。用中文输出。"
    "不要遗漏任何重要的事实或数字。"
)
