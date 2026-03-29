# ChatFlow — 本地 LLM 智能对话系统

基于 LangChain + LangGraph 构建的本地 AI 对话系统，支持多意图路由、自动规划、工具调用、长期记忆和工作流编排。

---

## 核心特性

- **多意图路由**：自动识别问题类型（代码 / 搜索 / 对话 / 搜索+代码），路由到对应模型
- **自动规划**：复杂任务自动拆分为多步骤执行计划，可视化展示进度
- **可编辑工作流**：前端直接拖拽、插入、删除执行步骤后重新执行
- **工具调用**：Web 搜索、网页阅读、时间查询、计算器，支持 MCP 协议扩展
- **三级记忆体系**：短期（滑动窗口）→ 中期（语义压缩摘要）→ 长期（Qdrant RAG 向量检索）
- **选择性遗忘**：话题切换时自动忽略无关历史，保持回答质量
- **流式 SSE 输出**：实时逐 token 渲染，工具调用进度实时展示

---

## 快速开始

### 方式一：Docker Compose 一键部署（推荐）

**前提**：
1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Windows/Mac）
2. 安装 [Ollama](https://ollama.com/download) 并下载模型：
   ```bash
   ollama pull qwen3:8b
   ollama pull bge-m3
   ```

**启动**：

```bash
cd llm-chat

# 复制配置文件（可选，有合理默认值）
cp .env.example .env
# 编辑 .env 按需修改模型名称等

# 一键启动三个容器（backend + frontend + qdrant）
docker compose up -d

# 查看日志
docker compose logs -f
```

浏览器访问 **http://localhost** 即可使用。

> **Windows Docker 说明**：容器内通过 `host.docker.internal` 访问宿主机上的 Ollama，已在 `docker-compose.yml` 中预设。

**停止**：
```bash
docker compose down
```

**重新构建**（代码变更后）：
```bash
docker compose up -d --build
```

---

### 方式二：本地手动启动（开发模式）

**前提**：Python 3.11+、Node.js 18+、Ollama 已运行

```bash
# 1. 下载模型
ollama pull qwen3:8b
ollama pull bge-m3

# 2. 启动后端
cd llm-chat/backend
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux
pip install -e .
python main.py

# 3. 启动前端（新终端）
cd llm-chat/frontend
npm install
npm run dev
```

访问 **http://localhost:5173**

---

## 配置说明

所有配置均支持通过 `.env` 文件或环境变量覆盖，无需修改代码。

```bash
cp .env.example .env
```

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | LLM 服务地址（支持 Ollama / vLLM / OpenAI） |
| `API_KEY` | `ollama` | API Key（OpenAI 填真实 Key） |
| `CHAT_MODEL` | `qwen3:8b` | 默认对话模型 |
| `EMBEDDING_MODEL` | `bge-m3` | Embedding 模型 |
| `ROUTER_ENABLED` | `true` | 是否启用意图路由 |
| `LONGTERM_MEMORY_ENABLED` | `true` | 是否启用 Qdrant 长期记忆 |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant 地址（Docker 内用 `http://qdrant:6333`） |

完整配置见 [`.env.example`](.env.example)。

---

## 项目结构

```
llm-chat/
├── backend/
│   ├── config.py          # 配置中心（pydantic-settings，支持 .env）
│   ├── main.py            # FastAPI 入口
│   ├── models.py          # Pydantic 请求/响应模型
│   ├── graph/             # LangGraph Agent 图
│   │   ├── agent.py       # 图构建与编译
│   │   ├── nodes.py       # 路由 / 规划 / 执行 / 反思节点
│   │   ├── edges.py       # 条件路由逻辑
│   │   ├── runner.py      # 流式执行 + SSE 事件生成
│   │   └── state.py       # AgentState 类型定义
│   ├── llm/               # LLM / Embedding 工厂
│   ├── memory/            # 短期 + 中期记忆
│   ├── rag/               # 长期记忆（Qdrant RAG）
│   ├── tools/             # 内置工具 + MCP 加载器
│   ├── Dockerfile
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── components/    # Vue 组件（Chat / CognitivePanel / MessageItem 等）
│   │   ├── composables/   # useChat 组合式函数
│   │   ├── types/         # TypeScript 类型定义
│   │   └── api/           # 后端 API 调用封装
│   ├── nginx.conf         # 生产 Nginx 配置
│   └── Dockerfile
├── docker-compose.yml     # 三容器编排
├── .env.example           # 配置模板
├── LICENSE
└── CHANGELOG.md
```

---

## 三级记忆体系

| 层级 | 机制 | 存储 |
|------|------|------|
| **短期记忆** | 滑动窗口（最近 10 轮） | 内存 + 磁盘 JSON |
| **中期摘要** | 达到触发轮数时自动压缩 | 磁盘 JSON |
| **长期记忆** | 压缩时写入 Qdrant，每轮检索 Top-K | Qdrant 向量库 |

**选择性遗忘**：当前问题与历史话题相似度低于阈值时，自动只发近 2 轮消息给模型，避免无关历史干扰。

---

## MCP 工具扩展

在 `.env` 中添加：

```bash
MCP_SERVERS={"filesystem":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","./data"],"transport":"stdio"}}
```

或直接在 `config.py` 的 `mcp_servers` 字典中配置。

---

## API 文档

后端启动后访问：**http://localhost:8000/docs**

主要接口：
- `POST /api/chat` — 流式对话（SSE）
- `GET /api/conversations` — 对话列表
- `GET /api/tools` — 可用工具列表
- `GET /api/conversations/{id}/memory` — 记忆状态调试

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)

## 许可证

[MIT License](LICENSE)
