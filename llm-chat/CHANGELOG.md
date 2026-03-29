# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)，变更记录格式参考 [Keep a Changelog](https://keepachangelog.com/)。

---

## [Unreleased]

### Added
- Docker Compose 一键部署（backend + frontend + Qdrant 三容器编排）
- 前端工作流节点支持任意位置插入（首节点前、任意两节点间、末尾追加）
- 工作流提问在对话框中渲染为专属卡片（显示目标和步骤列表）
- 多次编辑工作流节点后手动触发重新执行（dirty banner 方案）
- `workflowGoal` 字段持久化原始用户目标，多次重执行时保持显示正确
- `pydantic-settings` 重构 `config.py`，支持 `.env` 文件和环境变量覆盖
- `.env.example` 配置模板文件
- `LICENSE`（MIT）
- `CONTRIBUTING.md` 开发贡献指南
- `CHANGELOG.md`（本文件）

### Changed
- 认知面板工作流节点视觉优化：紧凑 AntV 风格，左侧彩色 accent 条
- 完成节点（done）显示绿色边框和背景，失败节点显示红色
- 节点间连接线高度从 18px 压缩至 10px
- dirty banner 重新设计为更紧凑的行内样式，包含撤销和重新执行按钮
- `pyproject.toml` 新增 `pydantic-settings>=2.3.0` 依赖

---

## [2.0.0] - 2025-03

### Added
- 基于 LangChain + LangGraph 的 Agent 图重构
- 多意图路由（code / search / chat / search_code）
- 规划节点（Planner）+ 反思节点（Reflector）
- 长期记忆（Qdrant RAG）
- MCP 工具加载器（stdio / SSE）
- 前端认知面板（实时展示计划、反思、追踪日志）
- 流式 SSE 工具调用进度展示
- 代码块预览（HTML / Vue / React 沙盒渲染）

### Changed
- 后端从单文件 Flask 迁移到 FastAPI + LangGraph

---

## [1.0.0] - 2024

### Added
- 初始版本：FastAPI 后端 + Vue 3 前端
- Ollama 本地模型接入
- 多轮对话 + 短期记忆
- Web 搜索工具（DuckDuckGo）
- 代码高亮渲染
