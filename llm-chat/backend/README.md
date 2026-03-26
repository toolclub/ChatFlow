# ChatFlow 后端 — 使用说明

> 基于 **LangChain + LangGraph** 构建的本地 AI 对话系统后端。
> 你不需要学过 LangChain，本文从零解释每一个概念。

---

## 目录

1. [快速启动](#一快速启动)
2. [整体架构图](#二整体架构图)
3. [LangChain 是什么（一句话版）](#三langchain-是什么一句话版)
4. [每层代码的作用](#四每层代码的作用)
5. [三级记忆系统详解](#五三级记忆系统详解)
6. [工具系统 Skills](#六工具系统-skills)
7. [MCP 是什么，怎么接入](#七mcp-是什么怎么接入)
8. [API 接口说明](#八api-接口说明)
9. [常见问题](#九常见问题)

---

## 一、快速启动

### 前提条件

| 软件 | 用途 | 下载 |
|---|---|---|
| Python 3.11+ | 运行后端 | python.org |
| Ollama | 在本地跑大模型 | ollama.com |
| Qdrant（可选） | 长期记忆向量数据库 | qdrant.tech |

### 第一步：下载模型

```bash
# 对话主模型
ollama pull qwen3-coder:30b

# 摘要模型（生成历史摘要用，可以换更小的）
ollama pull qwen2.5-coder:14b

# 向量嵌入模型（长期记忆用）
ollama pull bge-m3
```

### 第二步：安装依赖

```bash
cd llm-chat/backend

# 方式一（推荐）：用 pyproject.toml
pip install -e .

# 方式二：用 requirements.txt
pip install -r requirements.txt
```

### 第三步：启动 Qdrant（可选，长期记忆需要）

```bash
# 用 Docker 一键启动
docker run -p 6333:6333 qdrant/qdrant

# 不想装 Qdrant？在 config.py 把这一行改为 False 即可
# LONGTERM_MEMORY_ENABLED = False
```

### 第四步：启动后端

```bash
cd llm-chat/backend
python main.py
```

启动成功后访问：
- **API 文档**：http://localhost:8000/docs
- **聊天接口**：http://localhost:8000/api/chat

---

## 二、整体架构图

```
用户发消息
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│                    main.py  (FastAPI)                       │
│   接收 HTTP 请求，返回 SSE 流式响应                           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              graph/  (LangGraph Agent)                      │
│                                                             │
│   ① retrieve_context  ←── 去 Qdrant 查相关历史              │
│          │                 去 memory/store 取对话历史        │
│          ▼                                                  │
│   ② call_model        ←── 把消息喂给 Ollama                 │
│          │                 AI 决定：直接回复 or 调用工具     │
│          ↓                                                  │
│   ③ [tools 循环]     ←── 执行工具（计算/搜索/MCP...）       │
│          │                 把结果再喂给 AI（可多轮）          │
│          ▼                                                  │
│   ④ save_response     ←── 把对话保存到磁盘                  │
│          │                                                  │
│          ▼                                                  │
│   ⑤ compress_memory  ←── 超过阈值时生成摘要                 │
└─────────────────────────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
    memory/          rag/           tools/
  对话存储          Qdrant         工具系统
  + 压缩器          向量检索       内置+MCP
```

---



### LangChain = AI 应用的"积木库"

就像 jQuery 之于前端，LangChain 提供了一堆现成的积木：
- 调用各种 AI 模型（OpenAI、Ollama、Anthropic...）统一用一套接口
- 定义"工具"让 AI 能调用（搜索、计算、查数据库...）
- 把多个步骤串起来（先搜索，再总结，再回答）

### LangGraph = 让 AI 能"循环思考"

普通 LangChain 是线性的（A→B→C）。
LangGraph 允许**循环**：AI 可以先调用工具，看结果，再决定要不要继续调别的工具，直到满意了再回复用户。

```
AI思考 → 需要搜索 → 搜索工具 → 拿到结果 → AI再思考 → 满意了 → 回复用户
          ↑___________________________________|  (可以循环多次)
```

这就是为什么 AI 现在能做复杂的多步任务。

---

## 四、每层代码的作用

### `config.py` — 所有配置的中枢

**你只需要改这一个文件**来调整系统行为。

```python
CHAT_MODEL = "qwen3-coder:30b"    # 换成你想用的模型
LONGTERM_MEMORY_ENABLED = True     # 不想用 Qdrant 就改成 False
MCP_SERVERS = { ... }              # 配置 MCP 工具服务器
```

---

### `main.py` — 网关，负责接收请求

只做一件事：把 HTTP 请求转给内部模块处理，再把结果返回给前端。

启动时按顺序做：
1. 从磁盘加载历史对话到内存
2. 连接 Qdrant（如果启用）
3. 加载 MCP 工具（如果配置了）
4. 构建 LangGraph Agent 图

---

### `graph/` — AI 大脑（最核心）

这是整个系统的"大脑"，用 LangGraph 实现了一个**会循环思考、会使用工具**的 Agent。

#### `graph/state.py` — Agent 的"工作台"

每次对话时，Agent 需要一个地方存放当前状态（就像草稿纸）：

```python
class GraphState:
    conv_id             # 这是哪个对话？
    user_message        # 用户说了什么？
    messages            # 目前积累的所有消息（含工具调用记录）
    long_term_memories  # 从向量库找到的相关历史
    forget_mode         # 话题切换了？要"忘记"历史吗？
    full_response       # AI 最终的回复
    compressed          # 这轮触发压缩了吗？
```

#### `graph/nodes.py` — 四个处理步骤

**节点 1：`retrieve_context`（检索上下文）**
- 去 Qdrant 搜索与本次问题最相关的历史对话
- 判断话题是否切换（用数学方法计算语义相似度）
- 组装好"给 AI 看的消息列表"：系统提示 + 历史摘要 + 相关记忆 + 近期对话

**节点 2：`call_model`（调用 AI）**
- 把组装好的消息喂给 Ollama
- AI 返回两种结果之一：
  - 有工具调用 → 转到工具节点执行
  - 纯文字回复 → 转到保存节点

**节点 3：`save_response`（保存对话）**
- 把用户消息和 AI 回复写入 JSON 文件（只写最终 user/assistant 对，工具调用中间过程不写）

**节点 4：`compress_memory`（压缩记忆）**
- 如果对话超过阈值（默认 16 条消息），自动总结旧消息生成摘要
- 同时把待摘要的消息存入 Qdrant（变成长期记忆）

#### `graph/edges.py` — 路由决策

```
AI 的回复里有工具调用指令？
  是 → 去执行工具 → 工具结果再喂给 AI → 继续循环
  否 → 去保存结果 → 压缩检查 → 结束
```

#### `graph/agent.py` — 组装图

把所有节点和边组装成完整的"流程图"，编译后得到可以运行的 Agent。
启动时调用 `init(tools)` 一次，之后每次请求复用同一个图（效率高）。

#### `graph/runner.py` — 翻译官

LangGraph 产生的是内部事件流，这里把它翻译成前端能读的 SSE 格式：

```
LangGraph 事件              →  SSE 输出
on_chat_model_stream        →  data: {"content": "AI回复的一个字"}\n\n
on_tool_start               →  data: {"tool_call": {"name":"calculator",...}}\n\n
on_tool_end                 →  data: {"tool_result": {"name":"calculator",...}}\n\n
图执行完毕                   →  data: {"done": true, "compressed": false}\n\n
```

> **重要过滤**：生成摘要时也会调用 AI（摘要模型），runner 只转发"对话模型节点 call_model"
> 产生的 token，摘要模型的 token 被过滤掉，不会出现在前端。

---

### `memory/` — 记忆管理

#### `memory/schema.py` — 数据结构

定义了两个数据类（就是数据的"模板"）：

```python
class Message:
    role: str        # "user" 或 "assistant"
    content: str     # 消息内容
    timestamp: float

class Conversation:
    id: str                  # 对话唯一 ID（如 "a1b2c3d4"）
    title: str               # 标题（取自第一条用户消息）
    system_prompt: str       # 这个对话专属的系统提示词
    messages: list[Message]  # 完整对话历史（永远不删除）
    mid_term_summary: str    # 旧消息的压缩摘要
    mid_term_cursor: int     # 游标：哪条消息之前已经摘要过了
```

#### `memory/store.py` — 对话存储

- 启动时把 `conversations/` 目录下所有 `.json` 文件读入内存字典
- 每次新增消息就同步写回磁盘（防止崩溃丢数据）
- 提供 `get()` / `create()` / `delete()` / `add_message()` 等操作

#### `memory/context_builder.py` — 消息组装器

把一个对话对象变成"给 AI 看的消息列表"。组装顺序（重要，影响 AI 的行为）：

```
1. SystemMessage  = 系统提示词 + 工具列表说明 + 历史摘要 + 长期记忆
2. HumanMessage   = 历史用户消息（取最近 10 轮）
3. AIMessage      = 历史 AI 回复（取最近 10 轮）
4. HumanMessage   = 本轮用户消息（最新的）
```

如果触发了"忘记模式"（话题突然切换），只保留最近 2 轮，摘要和长期记忆都不发送。

#### `memory/compressor.py` — 对话压缩器

当未摘要的消息 ≥ 16 条时触发：
1. 取游标到滑动窗口起点之间的消息（就是"需要归档的旧消息"）
2. 把这些消息批量存入 Qdrant（变成长期记忆）
3. 用摘要模型把这批消息压缩成几百字的摘要
4. 更新游标，持久化到磁盘

---

### `rag/` — 长期记忆（RAG）

RAG = **R**etrieval-**A**ugmented **G**eneration，"检索增强生成"。
通俗理解：让 AI 在回答前先去"查笔记"，而不是只靠自己的记忆。

#### 工作原理

```
存入：
  对话消息 → bge-m3 向量模型 → 1024 个数字（向量）→ 存入 Qdrant

取出：
  用户新问题 → 转成向量 → 在 Qdrant 里找最相似的历史 → 取回原始文字 → 塞进 AI 上下文
```

#### `rag/retriever.py` — 检索 + 忘记模式判断

- `search_memories()` — 检索最相关的历史 Q&A 对
- `is_relevant_to_summary()` — 计算问题与历史摘要的相似度（决定是否忘记）
- `is_relevant_to_recent()` — 无摘要时，与最近几条消息做相似度比较

#### `rag/ingestor.py` — 批量写入

压缩触发时，把待摘要的消息对（用户问题 + AI 回答）批量写入 Qdrant。

---

### `llm/` — AI 模型工厂

#### `llm/chat.py`

提供两个 AI 模型实例（模块级缓存，不重复创建）：
- `get_chat_llm()` — 对话模型（qwen3-coder:30b），正常聊天用
- `get_summary_llm()` — 摘要模型（qwen2.5-coder:14b），压缩历史用

#### `llm/embeddings.py`

把文字转成数字向量，用于相似度计算和 Qdrant 存储。
`embed_text("你好")` → `[0.12, -0.34, 0.56, ...]`（1024 个数字）

---

### `tools/` — 工具系统（Skills）

工具让 AI 能"走出对话框"，做真实世界的操作。

#### 已内置的工具

| 工具名 | 功能 | 示例问题 |
|---|---|---|
| `calculator` | 数学计算（安全，不用 eval） | "计算 2 的 32 次方" |
| `get_current_datetime` | 查当前时间（支持时区） | "现在几点了" |
| `web_search` | DuckDuckGo 搜索（无需 API Key） | "搜索最新的 Python 版本" |

#### `tools/__init__.py` — 工具注册中心

```python
get_all_tools()    # 返回全部工具（内置 + MCP + 动态注册）
register_tool(t)   # 运行时动态添加工具
get_tool_names()   # 返回工具名称列表
get_tools_info()   # 返回工具详情（供 /api/tools 接口使用）
```

#### `tools/mcp/loader.py` — MCP 工具加载器

启动时连接 `config.py` 里配置的 MCP 服务器，自动把 MCP 工具变成普通 LangChain 工具。

---

## 五、三级记忆系统详解

这是本项目最核心的设计，解决了"AI 健忘"的问题。

```
┌─────────────────────────────────────────────────────────┐
│  第 1 级：短期记忆（滑动窗口）                            │
│  最近 10 轮对话，直接放入上下文                           │
│  [用户A][AI][用户B][AI]... 最多 20 条消息                 │
└─────────────────────────────────────────────────────────┘
              当消息数 ≥ 16 条时触发压缩 ↓
┌─────────────────────────────────────────────────────────┐
│  第 2 级：中期记忆（滚动摘要）                            │
│  旧消息被摘要模型压缩成几百字的文字摘要                   │
│  每次对话前，摘要以 system 消息形式注入                   │
│  "用户之前讨论了 Python 学习路径，偏好视频教程..."        │
└─────────────────────────────────────────────────────────┘
              压缩时同步写入 ↓
┌─────────────────────────────────────────────────────────┐
│  第 3 级：长期记忆（向量数据库 Qdrant）                   │
│  每对 Q&A 向量化后存储                                    │
│  每次对话前，用当前问题搜索最相似的历史记录               │
│  "3 个月前你问过同类问题，AI 当时的回答是..."             │
└─────────────────────────────────────────────────────────┘
```

### 忘记模式

有时候用户突然换话题（"好了不聊代码了，帮我写首诗"），这时把历史全塞进去反而干扰 AI。

系统会计算：新问题与历史摘要的语义相似度 < 0.4（可在 config.py 配置）？

- 相似 → 正常流程，注入所有记忆
- 不相似 + RAG 也没找到相关内容 → 触发**忘记模式**，只发最近 2 轮，减少干扰

---

## 六、工具系统 Skills

### 添加一个新工具（3 步）

**第一步**：创建 `tools/builtin/my_tool.py`：

```python
from langchain_core.tools import tool

@tool
def translate_text(text: str, target_language: str = "英文") -> str:
    """
    将文本翻译成指定语言。
    这段 docstring 非常重要——AI 会读这段话来决定什么时候调用此工具。
    描述越清晰，AI 越知道何时该用它。

    Args:
        text: 要翻译的文字
        target_language: 目标语言，如"英文"、"日文"、"法文"

    Returns:
        翻译结果
    """
    # 这里写你的翻译逻辑，比如调用翻译 API
    return f"翻译结果: {text} → ({target_language})"
```

**第二步**：在 `tools/builtin/__init__.py` 注册：

```python
from tools.builtin.my_tool import translate_text   # 新增这行

BUILTIN_TOOLS = [
    calculator,
    get_current_datetime,
    web_search,
    translate_text,   # 新增这行
]
```

**第三步**：重启服务，AI 就能自动在合适的时候调用这个工具了。

---

## 七、MCP 是什么，怎么接入

### MCP 简介

MCP = **M**odel **C**ontext **P**rotocol，Anthropic 提出的标准协议。
相当于给 AI 工具定义了一个**通用插头**——只要工具实现了这个协议，任何 AI 应用都能直接使用，不需要为每个应用单独写适配代码。

目前有大量现成的 MCP 服务器可用，比如：
- 文件系统操作（读写本地文件）
- Git 操作（提交、查看历史）
- 浏览器控制
- 数据库查询
- Slack / GitHub / Notion 集成

### 接入方式（零代码，只改配置）

修改 `config.py`，重启即可：

```python
MCP_SERVERS = {
    # 示例一：文件系统（需要 Node.js 环境）
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/my_documents"],
        "transport": "stdio",   # stdio = 启动一个子进程
    },

    # 示例二：Git 操作（需要安装 uv: pip install uv）
    "git": {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "D:/my_project"],
        "transport": "stdio",
    },

    # 示例三：连接远程 MCP 服务器
    "my_remote_server": {
        "url": "http://localhost:8080/sse",
        "transport": "sse",   # sse = 连接远程 HTTP 服务器
    },
}
```

启动后 AI 就能使用这些工具，调用方式与内置工具完全一样。

---

## 八、API 接口说明

完整文档：http://localhost:8000/docs

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/models` | 获取 Ollama 中已下载的模型列表 |
| GET | `/api/conversations` | 获取所有对话列表（按时间倒序） |
| POST | `/api/conversations` | 创建新对话 |
| GET | `/api/conversations/{id}` | 获取对话详情（含完整消息历史） |
| PATCH | `/api/conversations/{id}` | 修改对话标题或系统提示词 |
| DELETE | `/api/conversations/{id}` | 删除对话（同时清除 Qdrant 记忆） |
| POST | `/api/chat` | **核心接口**，流式聊天（SSE） |
| GET | `/api/tools` | 获取当前可用工具列表 |
| GET | `/api/conversations/{id}/memory` | 调试：查看记忆状态 |
| POST | `/api/embedding` | 测试向量化接口 |

### 聊天接口示例

请求：
```json
POST /api/chat
{
    "conversation_id": "a1b2c3d4",
    "message": "帮我计算一下 2 的 32 次方",
    "model": "qwen3-coder:30b",
    "temperature": 0.7
}
```

响应（Server-Sent Events，一行行流式到达）：
```
data: {"content": "好的，"}
data: {"tool_call": {"name": "calculator", "input": {"expression": "2**32"}}}
data: {"tool_result": {"name": "calculator", "output": "2**32 = 4294967296"}}
data: {"content": "2 的 32 次方等于 **4,294,967,296**。"}
data: {"done": true, "compressed": false}
```

---

## 九、常见问题

**Q: 修改了 `config.py` 需要重启吗？**
需要，配置在启动时一次性读取。

**Q: 对话历史存在哪里？**
`conversations/` 目录下，每个对话一个 `.json` 文件，可以直接查看和备份。

**Q: 不想用 Qdrant 怎么办？**
在 `config.py` 中设置 `LONGTERM_MEMORY_ENABLED = False`，系统仍正常运行，只是没有第三级长期记忆。短期记忆和中期摘要不受影响。

**Q: AI 调用工具时前端会看到吗？**
会，SSE 流会发送 `tool_call` 和 `tool_result` 事件。如果前端暂时不处理这些字段，直接忽略即可，不影响正常显示。

**Q: 为什么用 qwen3-coder 而不是普通 qwen3？**
Coder 版本对工具调用（function calling）的支持更好，能更准确地决定何时调用工具、传什么参数。

**Q: 添加工具后 AI 不调用怎么办？**
检查工具的 docstring（函数说明注释）是否清晰。AI 依靠这段描述来判断工具的用途，描述模糊会导致 AI 不知道该用它。

**Q: MCP 服务器需要什么环境？**
- `stdio` 类型（command）：需要对应的运行时，如 Node.js（npx）或 Python uv
- `sse` 类型（url）：只需对方服务器在运行，本地无额外依赖

 LangGraph 工具调用完整原理        
                                                                                                                                                                                        
  1. 整体运行流程                                                                                                                                                                       
                                                                                                                                                                                          # 每次用户发消息，图从头跑一遍：                                                                                                                                                      
  START                                                                                                                                                                                 
    → route_model      # 选模型                                   
    → retrieve_context # 拼提示词
    → call_model       # 调 LLM
    → (有工具调用?)
        是 → tools → call_model (循环)
        否 → save_response → compress_memory
    → END

  ---
  2. 提示词是怎么拼的（context_builder.py）

  # build_messages() 最终拼出的消息列表长这样：

  [
    SystemMessage("""
      你是一个有用的AI助手...           ← 系统提示
      你拥有以下工具：web_search...      ← 工具说明（自动注入）
      【对话背景摘要】用户在开发一个...   ← 中期记忆摘要
      【长期记忆】上次用户提到过...       ← Qdrant 检索结果
    """),

    HumanMessage("上次那个bug修好了吗"), # 历史第N-9轮
    AIMessage("修好了，用了..."),
    HumanMessage("帮我搜一下最新价格"),  # 本轮用户消息（最后追加）
  ]

  这个列表整体送给 LLM，LLM"看到"完整上下文再回答。

  ---
  3. 工具调用的核心机制

  关键在 bind_tools，它告诉 LLM 有哪些工具可用：

  llm = ChatOllama(model="mistral-nemo")
  llm_with_tools = llm.bind_tools([web_search, calculator])

  # bind_tools 做的事：把工具的 schema 注入到请求里
  # 发给 Ollama 的实际请求长这样：
  {
    "model": "mistral-nemo",
    "messages": [...],
    "tools": [                          # ← bind_tools 加的
      {
        "type": "function",
        "function": {
          "name": "web_search",
          "description": "使用 DuckDuckGo 搜索...",
          "parameters": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"}
          }
        }
      }
    ]
  }

  LLM 看到工具列表后，可以选择：
  - 普通回复：直接输出文字
  - 调用工具：输出一个结构化的 tool_calls 对象（不是文字）

  ---
  4. should_continue — 路由判断

  # edges.py
  def should_continue(state: GraphState) -> str:
      last = state["messages"][-1]  # 取 LLM 最后一条输出

      if hasattr(last, "tool_calls") and last.tool_calls:
          return "tools"        # LLM 要调工具 → 去 tools 节点
      return "save_response"    # LLM 直接回答 → 去保存

  LLM 输出的两种情况：

  # 情况A：直接回答（AIMessage，content 有内容）
  AIMessage(
      content="最新价格是 3999 元",
      tool_calls=[]              # 空的
  )
  # → should_continue 返回 "save_response"

  # 情况B：要调工具（AIMessage，content 为空，tool_calls 有值）
  AIMessage(
      content="",
      tool_calls=[{
          "name": "web_search",
          "args": {"query": "iPhone 16 最新价格 2026"}
      }]
  )
  # → should_continue 返回 "tools"

  ---
  5. ToolNode — 工具实际执行

  tool_node = ToolNode([web_search, calculator])
  # ToolNode 是 LangGraph 内置的，不用自己写
  # 它做的事：
  #   1. 读取 state.messages[-1].tool_calls
  #   2. 找到对应的工具函数
  #   3. 传入参数并执行
  #   4. 把结果包装成 ToolMessage 追加到 messages

  执行后 state.messages 变成：

  [
    SystemMessage(...),
    HumanMessage("帮我搜一下最新价格"),
    AIMessage(content="", tool_calls=[{"name":"web_search","args":{...}}]),
    ToolMessage(content="搜索结果：1.苹果官网... 2.京东...")  # ← ToolNode 追加的
  ]

  ---
  6. 工具结果怎么回到用户

  ToolNode 执行完后，边连回 call_model：

  graph.add_edge("tools", "call_model")  # 工具完成 → 再次调 LLM

  LLM 第二次调用时，消息列表里已经有了工具结果：

  # LLM 第二次看到的消息列表：
  [
    SystemMessage(...),
    HumanMessage("帮我搜一下最新价格"),
    AIMessage(content="", tool_calls=[...]),     # 第一次输出（要调工具）
    ToolMessage(content="搜索结果：..."),          # 工具执行结果
    # LLM 看到这些后，综合搜索结果，生成最终回答：
  ]
  # LLM 第二次输出：
  AIMessage(content="根据搜索结果，最新价格是...", tool_calls=[])
  # tool_calls 为空 → should_continue → "save_response" → 结束

  ---
  7. save_response 是做什么的

  async def save_response(state: GraphState) -> dict:
      conv_id = state["conv_id"]
      user_msg = state["user_message"]
      full_response = state.get("full_response", "")

      # 只把 user/assistant 这一对写入持久化存储
      # 工具调用的中间过程（AIMessage tool_calls + ToolMessage）不存
      memory_store.add_message(conv_id, "user", user_msg)
      memory_store.add_message(conv_id, "assistant", full_response)

  为什么不存工具中间过程？

  存的：
    user:      "帮我搜一下最新价格"
    assistant: "根据搜索结果，最新价格是..."

  不存的（只在图内流转，用完丢弃）：
    AIMessage(tool_calls=[...])
    ToolMessage("搜索结果：...")

  这样下次对话加载历史时，消息列表干净整洁，不会把工具中间过程也送给 LLM。

  ---
  完整调用链 Demo

  用户："帮我查一下 iPhone 16 价格"

  ① route_model
     qwen3:1.7b → "search" → state.model = "mistral-nemo"

  ② retrieve_context
     messages = [SystemMessage(...工具说明+记忆...), HumanMessage("帮我查...")]

  ③ call_model (第1次)
     mistral-nemo 看到工具列表，决定调 web_search
     → AIMessage(tool_calls=[{name:"web_search", args:{query:"iPhone 16价格 2026"}}])
     → should_continue → "tools"

  ④ ToolNode
     执行 web_search("iPhone 16价格 2026")
     → ToolMessage("1.苹果官网 3999元 2.京东 3799元...")
     → 追加到 messages

  ⑤ call_model (第2次)
     mistral-nemo 看到搜索结果，生成最终回答
     → AIMessage(content="根据最新搜索，iPhone 16 官方价格 3999 元起...")
     → should_continue → "save_response"

  ⑥ save_response
     写入: user="帮我查..." / assistant="根据最新搜索..."

  ⑦ compress_memory
     检查是否需要压缩，不需要 → 结束

  ⑧ runner.py 全程旁观 astream_events，把每个阶段实时推给前端

