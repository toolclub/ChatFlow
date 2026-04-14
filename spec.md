# ChatFlow 开发规格

> **DB 驱动 + 状态机 + 模型无关 + 全链路流式**

---

## 铁律（违反必出 bug）

1. **不从模型输出文本推断状态** — 用 DB 字段（`tool_executions.status`、`clarification_data`）
2. **不在 `content` 中嵌入结构化数据** — 用独立字段（`tool_summary`、`step_summary`）
3. **不硬编码 SSE 事件类型** — 用 `fsm/sse_events.py` 的 `SSEEventType` 枚举
4. **不截断当前轮消息列表** — 历史窗口由 `context_builder` 控制，工具调用的 AIMessage↔ToolMessage 对必须完整（否则 MiniMax 报 2013）
5. **不在同步函数中 await** — 所有 DB 查询放在 `async def` 中
6. **所有 LLM 调用必须流式** — `_stream_tokens` / `_stream_tokens_with_tools`，禁用 `ainvoke`
7. **状态变更必须走状态机** — `StreamSession._conv_sm.send_event()` → 再持久化到 DB
8. **不原地修改 plan 列表** — `_mark_step()` 返回新列表，LangGraph `add_messages` reducer 是 append-only，直接赋值 `messages=` 会崩
9. **不吞掉异常** — `except Exception: pass` 必须至少 `logger.warning`，否则 DB 写入失败、步骤状态卡死都查不出来

---

## 模型思考流程（`llm_thinking` 事件全链路）

整个会话过程中，模型的思考（thinking/reasoning）通过 `llm_thinking` 自定义事件实时推送到前端，
前端在消息或步骤的"思考"折叠区展示，用户可随时展开查看。

```
节点              thinking 来源                    前端展示位置            约束
─────────────────────────────────────────────────────────────────────────────────
route_model       路由 LLM 的 reasoning_content    message.thinking       无特殊约束
planner           规划 LLM 的 reasoning + content  message.thinking       ⚠️ 禁止思考中写代码
call_model        推理 LLM 的 reasoning_content    step.thinking 或       正常推理
                                                   message.thinking
call_model_       工具后 LLM 的 reasoning_content  step.thinking 或       正常推理
after_tool                                         message.thinking
reflector         评估 LLM 的 reasoning + content  message.thinking       仅边缘场景调用 LLM
```

**铁律补充**：
- planner 节点的 prompt 必须明确禁止模型在思考中写代码（`prompts/nodes/planner.md`），
  否则模型会在 thinking 阶段生成完整 HTML/代码，浪费 10-30 秒，然后在执行阶段再写一遍。
- 所有节点的 thinking 和 content 都必须推送 `llm_thinking` 事件，保证全流程可观测。
- 前端通过 `event.node` 字段区分来自哪个节点（`planner`/`call_model`/`reflector` 等）。

---

## 关键路径

### 对话生命周期（`fsm/conversation.py`）

```
ACTIVE ─start_stream→ STREAMING ─complete→ COMPLETED ─new_round→ STREAMING
                         ├─fail→ ERROR ─────────────new_round→ STREAMING
                         └─stop→ ACTIVE
```

`StreamSession` 持有 `_conv_sm` 实例贯穿整个会话，`_set_done()` 通过它驱动转换后才写 DB。

### 工具执行（`fsm/tool_execution.py`）

```
RUNNING ─finish→ DONE | ─fail→ ERROR | ─expire→ TIMEOUT
```

`_track_sse_for_db` 收到 `tool_result` 时创建 `ToolExecutionSM` 实例做转换，结果写 DB。`SandboxFormatter` 从 `exit_code` 判断 done/error。

### 计划步骤（`fsm/plan_step.py`）

```
PENDING ─start→ RUNNING ─finish→ DONE
                         ─fail──→ FAILED
```

`_mark_step()` 通过 `PlanStepSM` 校验状态转换后写 DB。`plan_steps.steps` JSONB 存储每步状态和结果。

DB 关联：
- `plan_steps.message_id` → 关联触发该计划的 assistant 消息
- `tool_executions.step_index` → 关联工具属于哪个计划步骤（0-based，无计划时 NULL）
- 前端刷新时通过 `step_index` 精确分发工具到对应步骤，通过 `plan.message_id` 匹配消息

### 消息写入时序

```
stream() 开始 → INSERT messages(stream_completed=False)
每 500ms      → UPDATE stream_buffer + thinking（_finalize_lock 保护）
图执行完成    → save_response_node UPDATE content + tool_summary + step_summary
              → _finalize_message 设 stream_completed=True（_finalized 幂等标记）
              → event_log 写入不受 _finalized 限制（done 事件必须持久化）
```

---

## 模型切换

改 `.env` 即可，不改代码：

```bash
LLM_BASE_URL="https://..."
CHAT_MODEL="claude-sonnet-4-20250514"
```

唯一硬性要求：**模型必须支持 OpenAI 兼容协议 + function calling**。

不兼容时在 `llm/client.py` 的 `astream()` 中加 5~10 行桥接（检测新字段、转换格式）。

所有 `# COMPAT:` 标记的代码对不适用的模型**空转无副作用**，换模型不会崩。

---

## 新增功能 checklist

| 做什么 | 改哪些文件 |
|--------|-----------|
| 新 DB 字段 | `db/models.py` 列 + `db/migrate.py` ALTER + `memory/schema.py` dataclass |
| 新状态 | `fsm/*.py` 枚举 + 转换 |
| 新 SSE 事件 | `fsm/sse_events.py` 枚举 + `_PRIORITY_ORDER` |
| 新工具 | `tools/builtin/` @tool 函数 + `tools/__init__.py` 注册 |
| 新图节点 | 继承 `BaseNode` + `graph/agent.py` 注册 + `edges.py` 路由 |
| **任何改动** | 对应模块测试 + `T-SMOKE-*` 冒烟（见 `test_case.md`） |

### 测试规范

- 测试用例文档：`backend/test_case.md`（只读此文件，不需要读测试代码）
- 测试代码：`backend/tests/`（Docker 镜像排除）
- **改代码必须跑对应模块的测试**，对照 `test_case.md` 末尾的"spec 对照检查"表
- 新增功能必须补充测试用例（先更新 `test_case.md`，再写测试代码）
- 标记：`@pytest.mark.unit`（无 IO）、`@pytest.mark.integration`（需 DB/Redis）、`@pytest.mark.smoke`（端到端）

### 新增产出文件的工具（重要）

工具如果会产出用户可下载的文件（打包、导出、截图等），只需后端两步，前端零改动：

```python
# 步骤 1：保存到 artifacts 表
await save_artifact(conv_id, name, path, content, language="archive", message_id=msg_id)

# 步骤 2：发 SSE 事件，必须带 downloadable: True
await adispatch_custom_event("file_artifact", {
    "name": name,
    "path": path,
    "language": "archive",   # archive/pptx/pdf 等
    "downloadable": True,    # ← 这个标记让前端自动显示下载卡片
    "message_id": msg_id,
})
```

前端自动处理：
- `MessageItem.vue`：`downloadable` 产物不需要文件名匹配，直接通过流式过滤
- `FileArtifactCard.vue`：自动显示下载按钮，走 `/api/artifacts/{id}/download`
- **不要**从 tool output 文本中正则提取文件名（违反铁律 #1）

---

## COMPAT 兼容层（标记了移除条件）

| 代码 | 依赖 | 何时可删 |
|------|------|---------|
| `<think>` 正则/状态机 | Qwen3/GLM 文本标签 | 模型 API 支持 `reasoning_content` |
| `[NEED_CLARIFICATION]` 解析 | system prompt 文本标记 | prompt 改用 tool_call 澄清 |
| `_TOOL_CALL_ARTIFACTS` 过滤 | MiniMax 流式残留 | MiniMax 修复 |
| `【工具调用记录】` 清理 | 旧数据嵌入 content | 旧消息全部压缩完 |
| JSON 文本提取（planner） | 模型不支持 JSON mode | 全模型支持 `response_format` |

---

## 踩过的坑（已修复）

- **语义缓存设计缺陷（已禁用，待重构）**：当前缓存把整个响应当纯文本存，忽略副作用（工具调用、文件产物、沙箱状态）。命中时跳过工具执行 → 产物丢失。临时修复（`_write_cache` 跳过工具响应 + 启动清缓存）不够彻底。已通过 `SEMANTIC_CACHE_ENABLED=false` 禁用。重构方向：只缓存纯知识问答（无工具调用），缓存 value 需含元数据标记（`has_tools`），命中时检查上下文一致性。
- **消息截断导致 2013**：`messages[-20:]` 截掉 AIMessage 留下 ToolMessage。已删除截断。
- **reflector 盲目 done**：最后一步工具失败但有 full_response 就判完成。已加 `_last_tool_failed()` 检查。
- **await 在同步函数**：`_inject_boundary` 是 `def`，DB 查询必须提到 `async` 调用方。
- **_finalized 挡住 event_log**：done 事件在 finalize 之后才入 batch。已将 event_log 写入移出锁。
- **sandbox 超时 30s**：mvn/gradle 需 60~120s。已改为 120s。
- **每步工具上限 3 次**：需要 5~6 次。已改为 6。
- **第一轮产物跑到第二轮**：`cognitive.artifacts` 全局共享不过滤。已按 toolCalls 文件名过滤。
- **`finalize_all_steps` 异常被 `pass` 吞**：步骤卡 running 查不出来。已改为 `logger.warning`。
- **对话被删但图还在跑**：`add_message` conv=None 静默返回。已加 warning 日志。
- **消息预写失败后继续跑图**：`pre_assistant_db_id=0` 导致 UPDATE 匹配 0 行。已加 try/except 中止流。
- **step_results 无限增长**：每步 result 可能 50KB+。已截断每条到 3000 字符。
- **步骤摘要只有 300 字**：步骤 2 看不到步骤 1 的关键信息。已改为 2000。
- **缓存中毒**：chat/code 路由缓存永不过期。已加 24h TTL 兜底（write-through 策略）。
- **SSH 连接假活**：`is_closed()` 返回 False 但不可用。已加 `echo ok` 探活命令。
- **forget_mode 跨重试不清除**：retry 时历史上下文仍被跳过。已在 retry 返回值中重置 `forget_mode: False`。
- **current_step_index 越界**：reflector 设超出范围的 index。已加 bounds check。
- **消费者崩溃后图任务泄漏**：queue 无人消费，内存增长。已在 finally 中 cancel `graph_task`。
- **planner 和 call_model 重复触发澄清**：两节点各自检查。planner 已设 `needs_clarification` 标记，call_model 检查跳过。
- **视觉分析中断丢全部结果**：50% 描述全丢。已加 try/except 保留部分描述。
- **工具参数生成 30 秒静默**：前端只收 ping。已加 `tool_call_args` 流式事件，终端实时显示代码生成。
- **用户猛刷新打垮后端**：无防抖，每次 F5 都发 full-state 请求。已加 2 秒防抖。
- **进程内状态跨 worker 不一致**：`_stop_events`/`_active_sessions`/`_store` 都是进程内 dict。已全部升级为 Redis 共享状态。
- **Redis 缓存失效 pop 竞态**：`_on_invalidate` 直接 pop 缓存，图执行中读取会 KeyError。已改为 `db_get_conversation` 刷新覆盖。
- **SSH 探活每次 +100ms**：健康连接也做 `echo ok`。已加 `_pool_last_used` 时间戳，30 秒内用过的跳过探活。
- **tool_call_args SSE 洪泛**：每个 JSON 片段都发一次。已加节流（200ms 或 500 字符一批）。
- **心跳 Redis 调用无超时**：Redis 慢时阻塞心跳循环。已加 `wait_for(timeout=2)`。
- **onToolCallArgs 找不到工具**：`_generating` 标记可能不存在。已加兜底 `findLast(t => !t.done)`。
- **`ToolExecutionStatus.RUNNING.value` AttributeError**：重构状态机时把 `ToolExecutionStatus` 从 Enum 误改为普通类（类属性是裸字符串没有 `.value`），`tool_store.py` 调 `.value` 崩溃 → 异常被 `_track_sse_for_db` 吞掉 → **所有 tool_execution 记录从未写入 DB** → 刷新后终端全部消失。已改回 `str, Enum` 枚举。教训：**状态枚举必须继承 `(str, Enum)`，不要用普通类模拟**。
- **`save_artifact` INSERT 后 id=0**：`session.add()` 后直接 `commit()` 没 `flush()`，新记录的自增 ID 拿不到 → SSE 事件带 `id:0` → 前端下载 `/api/artifacts/0/download` 返回空。已改为先 `flush()` 再 `commit()`。
- **沙箱会话跨 worker 丢失**：`_sessions` 是进程内 dict，刷新后落到不同 worker → 分配新沙箱 → 原文件找不到。已加 `conversations.sandbox_worker_id` DB 字段持久化。
- **`_get_worker_for_session` 用同步 DB 引擎**：为了同步读 DB 引入了 psycopg 第二套连接池，容器没装驱动就崩。已改为 async（所有调用方本来就是 async），砍掉同步引擎。
- **命令注入**：`sandbox_download` 的 `path` 参数直接拼进 shell 命令，特殊字符可执行任意命令。已加 `shlex.quote()` + 路径清理（禁 `..` 和绝对路径）。
- **下载 API 非 ASCII 文件名乱码**：`Content-Disposition: filename="中文.tar.gz"` 不符合 RFC 5987。已改为 `filename*=UTF-8''` 编码。

---

## 已知风险（未修复，改代码时务必注意）

### 开发者禁令（代码正确，但改错就崩）

| 禁令 | 位置 | 后果 |
|------|------|------|
| `_mark_step` 不可改为原地修改 | reflector_node/base.py | 步骤状态丢失，多步死循环 |
| 节点不可返回 `{"messages": 完整列表}` | graph/state.py `add_messages` reducer | LangGraph 类型错误崩溃，只能追加 |

### 中等风险

| 风险 | 位置 | 触发条件 | 后果 |
|------|------|---------|------|
| SSE tool_call 先于 DB INSERT | stream.py | 客户端收到事件后立即刷新 | full-state API 已兜底，刷新一次恢复 |
| 双重超时竞争 | worker.py watchdog + wait_for | 两个 120s 超时同时到 | watchdog kill 后 wait 拿 exit_status，实测无冲突 |
| `_TOOL_CALL_ARTIFACTS` 误杀正常内容 | save_response_node | 模型输出恰好含 `[TOOL_CALL]` 文本 | 概率极低，有 COMPAT 标记 |
| `_stream_tokens` 部分内容当完整保存 | base.py | LLM 流中途审核切断 | `_save_partial` 已加 `[回复中断]` 标记 |

### 进程级（已通过 Redis 解决）

| 原风险 | 解决方案 | 位置 |
|--------|---------|------|
| `_stop_events` 进程内 dict | Redis key `chatflow:stop:{conv_id}` + 心跳检测 | `db/redis_state.py` + `main.py` + `stream.py` |
| `_active_sessions` 进程内 dict | Redis key `chatflow:streaming:{conv_id}` 带 TTL + 心跳续期 | `db/redis_state.py` + `stream.py` |
| `_store` 内存缓存跨 worker 过期 | Redis pub/sub `chatflow:cache_invalidate` 通知失效，本 worker pop 缓存 | `db/redis_state.py` + `memory/store.py` |

原则：**永远不信任进程内数据**。所有跨 worker 共享状态走 Redis，本地 dict 仅做热路径加速，Redis 不可用时降级。
