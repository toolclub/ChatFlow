"""
集中管理所有配置，方便后续调整。
"""

# ── LLM 服务（OpenAI 兼容格式） ──
API_BASE_URL = "http://localhost:11434/v1"
API_KEY = "ollama"  # Ollama 不验证 key，随便填

# ── 模型配置 ──
CHAT_MODEL = "qwen2.5:14b"           # 对话主模型
SUMMARY_MODEL = "qwen2.5:1.5b"       # 摘要压缩模型
EMBEDDING_MODEL = "nomic-embed-text"  # Embedding 模型（预留）

# ── 上下文窗口 ──
CHAT_NUM_CTX = 4096                   # 对话模型上下文大小
SUMMARY_NUM_CTX = 2048                # 摘要模型上下文大小

# ── 记忆参数 ──
SHORT_TERM_MAX_TURNS = 10             # 短期记忆保留的最大轮数
COMPRESS_TRIGGER = 8                  # 触发压缩的轮数阈值
MAX_SUMMARY_LENGTH = 500              # 摘要最大字数

# ── 服务端口 ──
BACKEND_HOST = "0.0.0.0"
BACKEND_PORT = 8000

# ── System Prompt ──
DEFAULT_SYSTEM_PROMPT = "你是一个有用的AI助手，请用中文回答用户的问题。回答要准确、清晰、有条理。"

SUMMARY_SYSTEM_PROMPT = (
    "你是一个专业的摘要助手。你的任务是把对话历史压缩成简洁的摘要。"
    "要求：保留关键信息、用户偏好、重要结论和待办事项。用中文输出。"
    "不要遗漏任何重要的事实或数字。"
)
