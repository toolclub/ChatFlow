# ChatFlow 前端

> Vue 3 + TypeScript + Vite 构建的现代单页应用

---

## 快速启动

```bash
cd llm-chat/frontend
npm install
npm run dev
```

访问 **http://localhost:5173**

---

## 技术栈

| 类别 | 技术选型 |
|------|----------|
| 框架 | Vue 3.5 (Composition API) |
| 语言 | TypeScript 5.7 |
| 构建工具 | Vite 6.2 |
| UI 库 | Element Plus 2.13 + @element-plus/icons-vue |
| 状态管理 | 自定义 Composable (无 Pinia/Vuex) |
| API 通信 | fetch + SSE 流式 |
| Markdown | marked 13.0 + highlight.js + KaTeX |
| 图可视化 | @antv/x6 3.1 |
| 文档预览 | docx-preview 0.3 + xlsx 0.18 |
| 样式 | CSS Variables + Scoped CSS |

---

## 项目结构

```
src/
├── App.vue              # 根组件（全局布局 + 认知面板控制）
├── main.ts              # 入口文件（Element Plus 注册）
├── api/index.ts         # API 层（SSE 流式通信）
├── composables/         # 状态管理 Composables
│   └── useChat.ts       # 核心对话状态管理
├── components/          # Vue 组件
│   ├── Sidebar.vue      # 对话列表侧边栏
│   ├── ChatView.vue     # 主对话视图
│   ├── CognitivePanel.vue # 认知面板
│   ├── InputBox.vue     # 输入框组件
│   ├── MessageItem.vue  # 单条消息渲染
│   ├── AgentStatusBubble.vue # AI 状态气泡
│   ├── FileArtifactCard.vue # 文件产物卡片
│   ├── ClarificationCard.vue # 澄清问询卡片
│   ├── PlanFlowCanvas.vue # 计划流程图
│   └── preview/         # 文件预览模块
├── types/index.ts       # TypeScript 类型定义
├── utils/               # 工具函数
└── style.css            # 全局样式（Bilibili 风格）
```

---

## 组件层次

### 顶层布局层

| 组件 | 职责 |
|------|------|
| `App.vue` | 全局布局（Sidebar + ChatView + CognitivePanel）、面板折叠控制、文件预览状态管理 |

### 核心功能层

| 组件 | 职责 |
|------|------|
| `Sidebar.vue` | 对话列表侧边栏，支持搜索、批量删除、重命名、暗色模式切换 |
| `ChatView.vue` | 主对话视图，消息列表、进度条、状态标签、意图胶囊 |
| `CognitivePanel.vue` | 认知面板，执行计划、工具调用日志、文件产物预览 |
| `InputBox.vue` | 输入框组件，Agent/Chat 模式切换、意图胶囊选配、文件上传 |

### 消息展示层

| 组件 | 职责 |
|------|------|
| `MessageItem.vue` | 消息渲染，Markdown/KaTeX/代码高亮、工具调用卡片、思考过程展示 |
| `AgentStatusBubble.vue` | AI 状态气泡动画 |
| `FileArtifactCard.vue` | 文件产物卡片 |
| `UploadedFilePreview.vue` | 用户上传文件预览 |
| `CodePreview.vue` | 代码沙盒预览 |
| `ClarificationCard.vue` | 澄清问询交互卡片 |
| `PlanFlowCanvas.vue` | 计划流程图可视化（@antv/x6） |

---

## 状态管理

采用 **自定义 Composable 函数**（非 Pinia/Vuex），核心在 `composables/useChat.ts`：

### 特点

- 使用 `reactive<Record<string, ConvState>>` 按对话 ID 分离状态
- 每个对话独立维护：messages、loading、agentStatus、cognitive、abortController
- 通过 `computed` 派生当前对话的状态，实现高效切换
- 支持 SSE 流式消息增量更新、刷新恢复（DB full-state API）
- 内置防抖策略：同会话 2s 内不重复请求、跨会话 last-wins 版本号机制

### 关键状态

| 状态 | 说明 |
|------|------|
| `CognitiveState` | 认知面板状态（plan、traceLog、artifacts、reflection） |
| `AgentStatus` | AI 状态（idle/routing/planning/thinking/tool/reflecting/saving/done） |
| `Message` | 消息模型（含 thinkingSegments、steps、toolCalls、clarification） |

---

## API 层设计

### 通信方式

**fetch + SSE（Server-Sent Events）流式响应**

### 核心 API 函数

**对话管理**：
- `fetchConversations()` - 获取对话列表
- `createConversation()` / `deleteConversation()` / `renameConversation()`
- `fetchFullState(convId)` - 从 DB 恢复完整状态（刷新恢复）

**消息流式通信**：
- `sendMessage()` - POST `/api/chat`，返回 SSE 流
- `resumeStream()` - SSE 重连恢复（支持 after_event_id 断点续传）
- `stopStreamWithToken()` - 停止握手机制（stop_token + SSE 确认）

**文件操作**：
- `uploadFile(convId, file)` - multipart/form-data 上传
- `fetchArtifactContent(id)` - 按需加载产物完整内容
- `fetchArtifactBlob(id)` - 下载原始字节（PDF/Excel/图片）

### SSE 事件处理

| 事件 | 说明 |
|------|------|
| `content` | 文本增量 |
| `thinking` | 结构化思考段（node/step_index/phase/delta） |
| `tool_call` / `tool_result` | 工具调用 |
| `status` / `route` / `plan_generated` / `reflection` | 状态流转 |
| `clarification` | 澄清问询卡片 |
| `sandbox_output` | 代码沙盒输出 |
| `file_artifact` | 文件产物推送 |

---

## 设计风格

### Bilibili 风格配色

基于 CSS Variables 设计系统：

```css
--cf-bg: #f4f5f7          /* 背景色 */
--cf-bili-blue: #00a1d6   /* 主色调 */
--cf-bili-pink: #fb7299   /* 强调色 */
--cf-bili-orange: #ff9a2e /* 次强调色 */
```

支持亮色/暗色模式切换（`body.dark` 类切换）。

### 特色设计元素

| 元素 | 说明 |
|------|------|
| 顶部渐变进度条 | 蓝粉双色渐变 |
| 状态标签胶囊 | 圆角动画 |
| 意图胶囊悬浮卡片 | PPT 主题画廊、研究档位网格 |
| 认知面板 | 可拖拽缩放 |
| 沙盒终端 | 实时输出流式显示 |

---

## 文件预览模块

支持 8 种文件类型在线预览：

| 类型 | 渲染器 |
|------|--------|
| 图片 (jpg/png/gif/webp) | 原生 `<img>` |
| PDF | pdf.js |
| Word 文档 | docx-preview |
| Excel | xlsx + handsontable |
| Markdown | marked |
| 代码 | highlight.js |
| HTML iframe | 沙盒隔离 |
| PPT (pptx) | pptxgenjs preview |

---

## TypeScript 类型定义

核心类型定义在 `types/index.ts`：

| 类型 | 说明 |
|------|------|
| `Message` | 消息模型（含 thinkingSegments、steps、toolCalls） |
| `ConversationInfo` / `ConversationDetail` | 对话信息 |
| `PlanStep` / `StepRecord` / `ToolCallRecord` | 计划步骤 |
| `ThinkingSegment` / `ThinkingEvent` | 结构化思考协议 |
| `ClarificationData` / `ClarificationItem` | 澄清问询协议 |
| `AgentStatus` / `CognitiveState` | 状态模型 |
| `FileArtifact` | 文件产物模型 |
| `SendPayload` | 发送请求结构 |

---

## 亮点功能

| 功能 | 说明 |
|------|------|
| **流式架构** | 全链路 SSE 设计，支持实时消息推送、断点续传、停止握手 |
| **认知面板** | Agent 模式下的计划可视化、工具调用追踪、产物预览一体化 |
| **意图胶囊** | PPT/研究/代码/创作四种预设模式，带主题画廊和档位选配 |
| **澄清协议** | 结构化问询卡片（单选/多选/文本输入），支持交互式澄清 |
| **刷新恢复** | 通过 full-state API 从 DB 恢复完整对话状态（含 SSE 断点） |
| **文件预览** | 模块化渲染器注册表，支持 8 种文件类型在线预览 |

---

完整架构文档请查看 **[主页 README](../../README.md)** 和 **[开发规格](../../spec.md)**。