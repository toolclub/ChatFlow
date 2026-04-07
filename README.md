# ChatFlow — AI 智能对话平台

> 基于 LangChain + LangGraph 构建的新一代 AI 对话系统，支持多意图路由、认知规划、工具调用、多模态理解、三级记忆与语义缓存。

---

## 演示视频

[![点击观看演示](封面图URL)](https://www.bilibili.com/video/BV1bUSZBzEQ7?buvid=YF4C14F4733206BB412BBF37AAC4BDCE11FA&from_spmid=dt.dt.0.0&is_story_h5=false&mid=Qg%2Bfk0sn%2BD%2FajWCc6LzprA%3D%3D&plat_id=504&share_from=ugc&share_medium=iphone&share_plat=ios&share_session_id=1A9CFC59-BA95-421C-8BB2-E4D9C137A7A0&share_source=COPY&share_tag=s_i&spmid=dt.dt.0.0&timestamp=1775481987&unique_k=NdCRSTf&up_id=52293300&vd_source=b6eda8c8d8e6e7dcff168b4fddffd897)

---

## 核心特性

### 智能路由与认知规划
- **四层意图路由**：自动识别 `chat` / `code` / `search` / `search_code`，每类任务分配最优模型
- **路由解耦规划**：复杂 code 任务（长度 > 150 字或含"分析/重构/首先/分多步"等信号词）同样触发多步计划，不再局限于 search 路由
- **自动任务拆解**：复杂请求自动生成 1-10 步执行计划，可视化展示实时进度
- **可编辑工作流**：前端直接插入、删除、修改执行步骤后重新运行
- **高效步骤反思**：Reflector 节点内置 5 条快速路径，~90% 场景无需调用 LLM 即可决策

### 工具调用
- **内置工具**：Web 搜索（DuckDuckGo）、网页抓取、计算器、时间查询
- **MCP 协议扩展**：通过 `.env` 零代码接入任意 MCP 服务器（filesystem、github、fetch 等）
- **中间步骤工具调用**：code 路由多步执行时，中间步骤也可调用工具（如查 API 文档）

### 多模态理解
- **图片输入**：支持粘贴 / 拖拽 / 上传，客户端自动压缩（1280px / 82% 质量）
- **视觉解耦**：VisionNode 先将图片转为文字描述，主模型只处理文字——"分析归分析，推理归推理"
- **视觉流式推送**：分析过程逐 token 推送到思考折叠块中

### 三级记忆体系
| 层级 | 机制 | 特性 |
|------|------|------|
| **短期** | 滑动窗口（最近 N 轮） | 长 AI 历史回复自动截断至 800 字，防止 token 浪费 |
| **中期** | 达到阈值自动语义压缩 | 保留滑动窗口完整性，只压缩窗口外的远期消息 |
| **长期** | 压缩时写入 Qdrant，每轮 Top-K 检索 | 去重过滤：关键词重叠率 > 55% 的记忆不重复注入 |

**渐进式遗忘**：话题切换时不是二元切换，而是梯度缩短历史窗口，保留最近几轮基本连贯性

### 多步执行上下文隔离
- **步骤 1+** 完全不读 `state["messages"]` 积累历史，改用聚焦上下文：总目标 + 前序步骤结果摘要 + 当前步骤指令
- **中间步骤**：专注执行者系统提示，防止模型提前生成最终产品
- **末步**：恢复对话自定义 system prompt，确保最终回复风格符合用户期望
- **step_results 累积**：每步完成结果写入 `step_results[]`，最终一起持久化到 DB，下次对话可见完整执行过程

### 断点续传
- **错误恢复**：图执行中断（`GraphRecursionError` 等）时，已生成的部分响应自动保存到 DB
- **Continue 按钮**：前端检测到 `can_continue` 信号时展示"继续"按钮
- **计划持久化**：每次对话最新执行计划存入 DB，刷新页面后认知面板自动恢复

### 语义缓存
- **秒级响应**：相似问题命中 Redis KNN 缓存，跳过全部 LLM 推理链路
- **四种隔离模式**：`user` / `prompt` / `global` / `conv`
- **TTL 策略**：`chat/code` 路由永不过期，`search` 类可配置过期时间

### 流式输出与实时反馈
- **Token 级别推流**：SSE 逐 token 渲染，thinking 推理块实时展示
- **心跳保活**：每 5s 发 ping，防止 nginx 超时断流
- **优雅降级**：MiniMax 等厂商内容审核触发时自动降级响应，不中断 SSE 流

---

## 技术栈

| 层次 | 技术 |
|------|------|
| **后端框架** | Python 3.11 · FastAPI · asyncio |
| **AI 编排** | LangChain · LangGraph |
| **前端框架** | Vue 3 · TypeScript · Vite |
| **关系数据库** | PostgreSQL 16（含 JSONB 原子更新） |
| **向量数据库** | Qdrant |
| **缓存** | Redis Stack（RediSearch KNN） |
| **部署** | Docker Compose · Nginx |

---

## 支持的模型

兼容任何 OpenAI 格式接口，可混合搭配：

| 提供商 | 典型模型 | 用途 |
|--------|---------|------|
| **Ollama（本地）** | qwen3、qwen2.5vl、bge-m3 | 全功能本地运行 |
| **OpenAI** | gpt-4o、text-embedding-3 | 高精度云端 |
| **智谱 GLM** | GLM-4、GLM-4.6V | 中文优化 + 视觉 |
| **MiniMax** | MiniMax-M2.7 | 长上下文 + 多模态 |
| **其他** | 任意 OpenAI 兼容接口 | 自由配置 |

---

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/your-org/ChatFlow.git
cd ChatFlow/llm-chat

cp .env.example .env
# 编辑 .env，填写 API Key 和模型名称

docker compose up -d
docker compose logs -f backend
```

浏览器访问 **http://localhost**

```bash
docker compose down                  # 停止
docker compose up -d --build         # 代码更新后重新构建
```

### 本地开发

```bash
# 后端
cd llm-chat/backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py

# 前端（新终端）
cd llm-chat/frontend
npm install && npm run dev
```

后端：**http://localhost:8000** · 前端：**http://localhost:5173** · API 文档：**http://localhost:8000/docs**

---

## 配置说明

```bash
cp .env.example .env
```

### LLM 接口

```env
LLM_BASE_URL=https://api.openai.com/v1
API_KEY=sk-...
CHAT_MODEL=gpt-4o
SUMMARY_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_BASE_URL=                         # 留空复用 LLM_BASE_URL

VISION_MODEL=gpt-4o                         # 视觉模型（留空跳过图片分析）
VISION_BASE_URL=
VISION_API_KEY=
```

### 路由与模型映射

```env
ROUTER_ENABLED=true
ROUTER_MODEL=gpt-4o-mini
SEARCH_MODEL=gpt-4o
ROUTE_MODEL_MAP={"chat":"gpt-4o","code":"gpt-4o","search":"gpt-4o","search_code":"gpt-4o"}
```

### 记忆参数

```env
SHORT_TERM_MAX_TURNS=10
COMPRESS_TRIGGER=8
MAX_SUMMARY_LENGTH=500
LONGTERM_MEMORY_ENABLED=true
QDRANT_URL=http://qdrant:6333
EMBEDDING_DIM=3072
LONGTERM_TOP_K=3
LONGTERM_SCORE_THRESHOLD=0.5
```

### 语义缓存

```env
SEMANTIC_CACHE_ENABLED=true
REDIS_URL=redis://redis:6379
SEMANTIC_CACHE_THRESHOLD=0.88
SEMANTIC_CACHE_NAMESPACE_MODE=user       # user / prompt / global / conv
SEMANTIC_CACHE_SEARCH_TTL_HOURS=12
```

### MCP 工具扩展

```env
MCP_SERVERS={"filesystem":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","./data"],"transport":"stdio"}}
```

完整配置见 [`llm-chat/.env.example`](llm-chat/.env.example)

---

## 架构图

### LangGraph 完整执行流程

```mermaid
flowchart TD
    START([用户消息]) --> CC

    CC["⚡ semantic_cache_check\n语义缓存（含图片直接跳过）"]
    CC -->|"命中 similarity ≥ threshold"| SR
    CC -->|"未命中"| VN

    VN["👁️ vision_node\n图片→文字描述\n流式推送思考过程"]
    VN --> RM

    RM["🔀 route_model\nchat / code / search / search_code\n空 choices 自动降级 search_code"]
    RM --> RC

    RC["📚 retrieve_context\nRAG 向量检索 + 历史滑动窗口\n长期记忆去重注入"]
    RC --> PL

    PL["📋 planner\n生成执行计划\nsearch / search_code 必触发\ncode 复杂任务也触发"]
    PL -->|"有计划"| CM
    PL -->|"无计划（chat/简单code）"| CM

    CM["🧠 call_model\ntool_model 推理\n步骤0: 全历史\n步骤1+: 聚焦隔离上下文"]
    CM -->|"返回 tool_calls"| TN
    CM -->|"无工具 + 有计划"| RF
    CM -->|"无工具 + 无计划"| SR

    TN["⚙️ ToolNode\n并发执行工具\nWeb搜索/网页抓取/计算器"]
    TN --> CMT

    CMT["🧠 call_model_after_tool\nanswer_model 综合工具结果\n非末步: 重建聚焦上下文\n末步: 注入历史步骤结果"]
    CMT -->|"还有 tool_calls"| TN
    CMT -->|"无工具 + 有计划"| RF
    CMT -->|"无工具 + 无计划"| SR

    RF["🔍 reflector\n步骤评估（5条快速路径）\n~90% 场景不调用 LLM"]
    RF -->|"continue→下一步"| CM
    RF -->|"retry→重试"| CM
    RF -->|"done"| SR

    SR["💾 save_response\n持久化消息 + step_results 摘要\n检测澄清意图"]
    SR --> CM2

    CM2["🗜️ compress_memory\n超阈值时压缩 → 写入 Qdrant"]
    CM2 --> END([END])

    style CC fill:#fef9c3
    style VN fill:#ede9fe
    style RM fill:#dbeafe
    style RC fill:#e0f2fe
    style PL fill:#fef3c7
    style CM fill:#dcfce7
    style TN fill:#f3e8ff
    style CMT fill:#dcfce7
    style RF fill:#fce7f3
    style SR fill:#e0f2fe
    style CM2 fill:#e0f2fe
```

---

### Reflector 决策快速路径

```mermaid
flowchart TD
    IN([reflector.execute]) --> P1

    P1{"无执行计划?"}
    P1 -->|是| D1["✅ done\n（无计划，直接完成）"]
    P1 -->|否| P2

    P2{"边界超限?\nidx>=total\n或 iters>=3"}
    P2 -->|是| D2["✅ done\n（强制完成）"]
    P2 -->|否| P3

    P3{"最后一步\n且有响应?"}
    P3 -->|是| D3["✅ done (fast)\n持久化步骤结果"]
    P3 -->|否| P4

    P4{"非最后步\n且有响应\n且首次执行?"}
    P4 -->|是| D4["▶️ continue (fast)\n注入下一步指令\n附前序步骤摘要\n⚡ 最常见路径"]
    P4 -->|否| P5

    P5{"无响应\n且可重试?"}
    P5 -->|是| D5["🔁 retry (fast)\n自动重试"]
    P5 -->|否| LLM

    LLM["🤖 LLM 评估\n仅重试中有响应\n的边缘场景"]
    LLM --> D6["done / continue / retry"]

    style D4 fill:#dcfce7,stroke:#16a34a
    style LLM fill:#fce7f3,stroke:#db2777
    style D1 fill:#f0fdf4
    style D2 fill:#f0fdf4
    style D3 fill:#f0fdf4
    style D5 fill:#eff6ff
```

---

### 三级记忆架构

```mermaid
flowchart LR
    subgraph 输入层
        UM["用户消息"]
    end

    subgraph 检索层["每轮对话: retrieve_context"]
        direction TB
        SW["短期记忆\n滑动窗口 N 轮\n长 AI 历史自动截断 800字"]
        MTS["中期摘要\nmid_term_summary\n远期对话压缩 blob"]
        LTM["长期记忆\nQdrant Top-K\n去重: 关键词重叠>55% 跳过"]
    end

    subgraph 压缩层["compress_memory（超阈值触发）"]
        direction TB
        COMP["语义压缩\nSUMMARY_MODEL\n窗口外消息 → 摘要"]
        QDRANT["写入 Qdrant\n向量化存储\n下次 RAG 检索"]
        COMP --> QDRANT
    end

    subgraph 遗忘层
        NF["正常模式\n全窗口历史"]
        FM["forget_mode\n渐进式缩短\n近 N 轮 + 不注入远期记忆"]
    end

    UM --> 检索层
    SW --> 遗忘层
    MTS -->|非 forget_mode| NF
    LTM -->|去重后| NF
    遗忘层 --> LLM["📤 发送给 LLM"]
    LLM --> 压缩层
```

---

### SSE 流式架构

```mermaid
sequenceDiagram
    participant B as 浏览器
    participant N as Nginx
    participant F as FastAPI
    participant Q as asyncio.Queue
    participant G as LangGraph Task
    participant H as Heartbeat Task

    F->>G: create_task(_graph_producer)
    F->>H: create_task(_heartbeat, 5s)
    
    loop running
        G-->>Q: put(event)
        H-->>Q: put(ping)
        F->>Q: await queue.get()
        F-->>N: SSE data
        N-->>B: stream push
    end

    alt success
        G-->>Q: put(done)
        F-->>B: {"done": true}
    else error
        G-->>Q: put(error)
        F-->>B: {"error": "...", "can_continue": true}
        Note over B: show continue button
    else disconnect
        B-->>F: TCP disconnect
        F->>G: cancel()
        F->>H: cancel()
    end
```

---

### 多步执行上下文隔离

```mermaid
flowchart LR
    subgraph 步骤0["步骤 0（首步）"]
        direction TB
        S0A["system_prompt\n+ mid_term_summary\n+ long_term_memories（去重）"]
        S0B["对话历史（滑动窗口）"]
        S0C["HumanMessage\n+ 步骤0指令注入"]
        S0A --> S0B --> S0C
    end

    subgraph 步骤N["步骤 1+（中间步骤）"]
        direction TB
        SNA["聚焦 system_prompt\n（专注执行者角色）"]
        SNB["HumanMessage(总目标)"]
        SNC["AIMessage(步骤0结果)\n...AIMessage(步骤N-1结果)"]
        SND["HumanMessage(当前步骤指令)"]
        SNA --> SNB --> SNC --> SND
    end

    subgraph 末步["末步（生成最终回复）"]
        direction TB
        ENA["对话自定义 system_prompt\n（保持风格人格）"]
        ENB["HumanMessage(总目标)"]
        ENC["AIMessage(步骤0结果)\n...AIMessage(步骤N-1结果)"]
        END2["HumanMessage\n请基于以上结果生成最终回复"]
        ENA --> ENB --> ENC --> END2
    end

    步骤0 -->|step_results 累积| 步骤N
    步骤N -->|step_results 累积| 末步

    style 步骤N fill:#fef3c7
    style 末步 fill:#dcfce7
```

---

### 请求完整链路

```mermaid
sequenceDiagram
    participant 浏览器
    participant Nginx
    participant FastAPI
    participant Runner
    participant LangGraph
    participant DB as PostgreSQL
    participant Qdrant
    participant Redis

    浏览器->>Nginx: POST /api/chat {message, images?}
    Nginx->>FastAPI: proxy_pass
    FastAPI->>Runner: stream_response()
    
    Runner->>Redis: Embedding + KNN 语义缓存查询
    alt 缓存命中
        Redis-->>Runner: 缓存答案
        Runner-->>浏览器: SSE cache_hit + content
    else 缓存未命中
        Runner->>Qdrant: RAG 向量检索 Top-K
        Runner->>DB: 查询对话历史 + 计划
        Runner->>LangGraph: astream_events(initial_state)
        loop 图执行
            LangGraph-->>Runner: 事件流
            Runner-->>浏览器: SSE 逐 token 推送
        end
        LangGraph->>DB: 保存消息 + step_results
        LangGraph->>Qdrant: 压缩写入向量
        LangGraph->>Redis: 写回缓存（非 search 路由）
        Runner-->>浏览器: SSE done
    end
```

---

## 项目结构

```
ChatFlow/
├── llm-chat/
│   ├── backend/
│   │   ├── main.py                         # FastAPI 入口 + API 路由
│   │   ├── config.py                       # 统一配置（pydantic-settings）
│   │   ├── graph/
│   │   │   ├── agent.py                    # LangGraph 图构建与编译
│   │   │   ├── state.py                    # GraphState（含 step_results 字段）
│   │   │   ├── edges.py                    # 条件路由逻辑
│   │   │   ├── event_types.py              # 节点输出类型定义
│   │   │   ├── nodes/
│   │   │   │   ├── base.py                 # BaseNode（_stream_tokens 等共享工具）
│   │   │   │   ├── vision_node.py          # 多模态图片理解（流式）
│   │   │   │   ├── route_node.py           # 意图路由（空 choices 防御）
│   │   │   │   ├── planner_node.py         # 认知规划（code 复杂任务也触发）
│   │   │   │   ├── retrieve_context_node.py # RAG + 历史组装
│   │   │   │   ├── call_model_node.py      # 主推理（步骤隔离上下文）
│   │   │   │   ├── call_model_after_tool_node.py # 工具后综合
│   │   │   │   ├── reflector_node.py       # 步骤评估（5条快速路径）
│   │   │   │   ├── save_response_node.py   # 持久化（step_results 摘要）
│   │   │   │   └── cache_node.py           # 语义缓存节点
│   │   │   └── runner/
│   │   │       ├── stream.py               # SSE 主驱动（队列+心跳+断点续传）
│   │   │       ├── context.py              # StreamContext
│   │   │       ├── dispatcher.py           # 事件分发（职责链）
│   │   │       └── handlers/               # 各类 SSE 事件处理器
│   │   ├── memory/
│   │   │   ├── store.py                    # 短期记忆 CRUD
│   │   │   ├── context_builder.py          # 消息组装（去重+渐进遗忘+历史截断）
│   │   │   └── schema.py                   # 数据模型
│   │   ├── rag/                            # 长期记忆（Qdrant）
│   │   ├── cache/                          # 语义缓存（Redis KNN）
│   │   ├── db/
│   │   │   ├── models.py                   # SQLAlchemy ORM
│   │   │   ├── database.py                 # 异步会话
│   │   │   └── plan_store.py               # 执行计划 CRUD（jsonb_set 原子更新）
│   │   ├── llm/                            # LLM / Embedding 工厂
│   │   ├── tools/                          # 内置工具 + MCP 加载器
│   │   └── Dockerfile
│   ├── frontend/
│   │   ├── src/
│   │   │   ├── components/
│   │   │   │   ├── ChatView.vue            # 主对话界面 + Continue 按钮
│   │   │   │   ├── MessageItem.vue         # 消息渲染（Markdown + 沙盒预览）
│   │   │   │   ├── InputBox.vue            # 输入框 + 图片管理
│   │   │   │   ├── CognitivePanel.vue      # 右侧认知面板（计划刷新恢复）
│   │   │   │   ├── PlanFlowCanvas.vue      # 任务流程图（@antv/x6）
│   │   │   │   ├── ClarificationCard.vue   # 澄清交互卡片
│   │   │   │   └── AgentStatusBubble.vue   # 工具调用状态气泡
│   │   │   ├── composables/useChat.ts      # 核心状态管理（canContinue + continueLast）
│   │   │   ├── api/index.ts                # 后端 API 封装（onInterrupted 回调）
│   │   │   └── types/                      # TypeScript 类型
│   │   ├── nginx.conf
│   │   └── Dockerfile
│   ├── docker-compose.yml                  # 五容器编排
│   └── .env.example
└── README.md
```

---

## API 接口

后端启动后访问 **http://localhost:8000/docs** 查看完整文档。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/chat` | 流式对话（SSE），支持 `images` 字段 |
| `POST` | `/api/chat/{id}/stop` | 停止对话 |
| `GET` | `/api/conversations` | 对话列表 |
| `POST` | `/api/conversations` | 创建对话 |
| `GET` | `/api/conversations/{id}` | 对话详情 |
| `DELETE` | `/api/conversations/{id}` | 删除对话 |
| `GET` | `/api/conversations/{id}/tools` | 工具调用历史 |
| `GET` | `/api/conversations/{id}/plan` | 最新执行计划（刷新恢复用） |
| `GET` | `/api/conversations/{id}/memory` | 记忆状态调试 |
| `GET` | `/api/tools` | 可用工具列表 |

---

## SSE 事件参考

前端通过 EventSource 接收以下事件：

| 事件字段 | 说明 |
|---------|------|
| `{"token": "..."}` | LLM 输出 token（逐字流式） |
| `{"thinking": "..."}` | 推理模型 thinking 内容（折叠展示） |
| `{"status": "cache_hit", "similarity": 0.92}` | 语义缓存命中 |
| `{"route": "search_code"}` | 路由决策结果 |
| `{"plan": [...]}` | 执行计划（含步骤标题和状态） |
| `{"step_result": {...}}` | 单步完成事件 |
| `{"tool_start": {...}}` | 工具调用开始 |
| `{"tool_end": {...}}` | 工具调用结果 |
| `{"done": true, "compressed": false}` | 流正常结束 |
| `{"error": "...", "can_continue": true}` | 执行出错，前端展示 Continue 按钮 |
| `{"ping": true}` | 心跳保活（前端忽略） |

---

## 许可证

[MIT License](LICENSE)
