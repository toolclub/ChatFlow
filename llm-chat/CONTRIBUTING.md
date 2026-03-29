# Contributing to ChatFlow

感谢你对 ChatFlow 感兴趣！本文档说明如何参与贡献。

## 开发环境搭建

### 后端

```bash
cd backend
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -e .
# 启动后端
python main.py
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

### 环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的模型地址和配置
```

## 项目结构

```
llm-chat/
├── backend/
│   ├── config.py          # 配置中心（pydantic-settings）
│   ├── main.py            # FastAPI 入口
│   ├── models.py          # Pydantic 请求/响应模型
│   ├── graph/             # LangGraph Agent 图
│   │   ├── agent.py       # 图构建与编译
│   │   ├── nodes.py       # 图节点（路由、规划、执行、反思）
│   │   ├── edges.py       # 图边（条件路由逻辑）
│   │   ├── runner.py      # 流式执行入口
│   │   └── state.py       # AgentState 定义
│   ├── llm/               # LLM 工厂
│   ├── memory/            # 短期/中期记忆
│   ├── rag/               # 长期记忆（Qdrant RAG）
│   └── tools/             # 内置工具 + MCP 加载器
└── frontend/
    ├── src/
    │   ├── components/    # Vue 组件
    │   ├── composables/   # useChat 等组合式函数
    │   ├── types/         # TypeScript 类型定义
    │   └── api/           # 后端 API 调用
    └── nginx.conf         # Docker 生产 Nginx 配置
```

## 提交规范

提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<type>(<scope>): <description>

feat(graph): 添加反思节点重试机制
fix(frontend): 修复工作流节点插入顺序错误
docs: 更新 README 部署说明
refactor(config): 迁移到 pydantic-settings
```

**type 类型：**
- `feat` — 新功能
- `fix` — Bug 修复
- `docs` — 文档更新
- `refactor` — 代码重构（不影响功能）
- `style` — 样式/格式调整
- `test` — 测试相关
- `chore` — 构建/依赖/配置变更

## Pull Request 流程

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feat/my-feature`
3. 提交变更（遵循提交规范）
4. 确保本地运行无明显错误
5. 发起 Pull Request，描述变更内容和动机

## 添加新工具

在 `backend/tools/builtin/` 下新建 `my_tool.py`：

```python
from langchain_core.tools import tool

@tool
def my_tool(param: str) -> str:
    """工具描述，Agent 据此决定何时调用。"""
    return f"结果: {param}"
```

然后在 `backend/tools/__init__.py` 中注册即可。

## 添加新路由意图

在 `backend/graph/nodes.py` 的路由节点中添加新的 intent，
并在 `config.py` 的 `route_model_map` 中映射对应模型。

## 代码风格

- **Python**：遵循 PEP 8，推荐使用 `ruff` 格式化
- **TypeScript/Vue**：遵循项目现有风格，组件使用 `<script setup>` 语法

## 问题反馈

请通过 GitHub Issues 提交 Bug 或功能建议，描述清楚复现步骤和期望行为。
