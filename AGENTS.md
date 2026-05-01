# ChatFlow AI Agent 指南 (AGENTS.md)

> **DB 驱动 + 状态机 + 模型无关 + 全链路流式**
> 本文档是为 Gemini, Claude, OpenCode 等 AI 助手设计的核心索引，总结了项目架构、开发铁律及关键路径，旨在减少上下文读取量并防止低级错误。

---

## 1. 核心铁律 (Ironclad Rules)
**违反以下规则必出严重 Bug：**

1.  **状态非文本推断**：禁止从 LLM 输出文本推断业务状态。**必须**使用 DB 字段 (`tool_executions.status`, `conversations.status`) 或状态机 (`fsm/`)。
2.  **内容数据分离**：禁止在 `messages.content` 中嵌入结构化数据（如工具输出）。**必须**使用 `tool_summary`, `step_summary` 等独立字段。
3.  **SSE 规范**：禁止硬编码 SSE 事件名。**必须**复用 `fsm/sse_events.py` 中的 `SSEEventType`。
4.  **DB-First & Async**：所有 IO 操作必须立即持久化。禁止在同步函数中 `await`。所有数据库操作必须使用异步连接 (`AsyncSessionLocal`)。
5.  **LLM 必流式**：除了后台分析任务，所有面向用户的 LLM 调用必须使用 `astream` 系列方法。
6.  **状态机驱动**：所有状态变更必须先通过 `fsm/` 中的状态机校验，再写入数据库。
7.  **思维链协议**：所有节点的思考过程必须通过 `BaseNode.emit_thinking(state, node, phase, delta)` 推送。

---

## 2. 架构概览

### 后端 (Backend: FastAPI + LangGraph)
- **Graph 引擎**：使用 `langgraph` 构建 Agent。
  - `planner`: 生成执行计划。
  - `call_model`: 执行单步推理（支持工具绑定）。
  - `reflector`: 自我反思并决定下一步（retry/continue/done）。
- **内存管理**：
  - `context_builder`: 负责消息分层（Layered System Prompts）。
  - `core_memory`: 存储项目规则、用户画像、偏好等。
- **持久化**：
  - PostgreSQL (SQLAlchemy Async): 存储消息、对话、工具执行、事件日志 (`event_log`)。
  - Redis: 存储跨 worker 共享状态（停止信号、流式锁定、缓存失效）。

### 前端 (Frontend: Vue 3 + Vite + Element Plus)
- **工作台模式**：
  - `ChatView`: 聊天工作台。
  - `QuantView`: 量化工作台（A 股选股分析）。
- **流式处理**：
  - `useChat.ts`: 消费 SSE 事件流，实时更新消息树、思维链和终端输出。
- **UI 风格**：仿 Bilibili/现代简约风格。

---

## 3. 关键模块说明

### 量化选股 (Quant)
- **流程**：Universe (股票池) → 属性过滤 → 因子计算 (技术/基本面/流动性) → 归一化评分 → Top N 排序。
- **Agent 接入**：选股后可点击“接入对话”，通过 `context_refs` 将选股快照 ID 传入对话，LLM 会查库获取数据进行深度分析。

### 执行计划 (Planning)
- 计划存储在 `plan_steps` 表。
- 模型在 `planner` 节点生成 JSON 计划。
- 只有在 `search`/`search_code` 等需要多步执行的路由下才会触发计划。

### 文件产物 (Artifacts)
- 工具产生的文件必须通过 `save_artifact` 存库。
- 发送 `file_artifact` SSE 事件（带 `downloadable: true`）触发前端下载按钮。

---

## 4. 目录地图
- `/backend/graph/`: Agent 核心图、节点 (`nodes/`)、边逻辑 (`edges/`)。
- `/backend/db/`: 模型定义 (`models.py`)、迁移 (`migrate.py`)、各模块 Store。
- `/backend/fsm/`: 核心业务状态机（对话、步骤、工具）。
- `/backend/quant/`: 选股算法、数据 Provider (baostock, akshare, tushare)。
- `/frontend/src/composables/`: 业务逻辑 (useChat, useQuant)。
- `/frontend/src/components/`: UI 组件。

---

## 5. 开发建议
1.  **修改前 Grep**：修改核心逻辑前，搜索所有引用点。
2.  **遵循 COMPAT**：看到带有 `# COMPAT:` 的代码，不要轻易修改或删除，除非移除条件满足。
3.  **测试先行**：阅读 `backend/test_case.md` 了解核心链路的预期行为。
4.  **日志规范**：确保使用 `logger.info` 或 `logger.error` 记录关键路径，禁止吞掉异常。

---
*Created by Gemini CLI on 2026-05-01*
