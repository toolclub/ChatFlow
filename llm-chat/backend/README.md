# ChatFlow 后端

完整架构文档、流程图、配置说明请查看：**[主页 README](../../README.md)**

---

## 本地开发快速启动

```bash
cd llm-chat/backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

API 文档：**http://localhost:8000/docs**

---

## 关键目录

```
backend/
├── graph/nodes/          # LangGraph 节点
│   ├── base.py           # BaseNode（共享 _stream_tokens / _is_audit_error）
│   ├── route_node.py     # 意图路由（空 choices 防御降级）
│   ├── planner_node.py   # 认知规划（code 复杂任务也触发）
│   ├── reflector_node.py # 步骤评估（5 条快速路径，~90% 无 LLM 调用）
│   └── call_model_node.py # 步骤隔离上下文（步骤1+不读全量 messages）
├── graph/runner/stream.py # SSE 驱动（断点续传 + 心跳）
├── memory/context_builder.py # 长期记忆去重 + 渐进遗忘 + 历史截断
└── db/plan_store.py      # 执行计划（jsonb_set 原子更新）
```
