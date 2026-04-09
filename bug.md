# ChatFlow Bug 记录

> 代码审查和测试中发现的问题。标记为 [已修复] 或 [遗留]。

---

## [遗留] 缓存 TTL 行为变更

**位置**：`save_response_node.py` `_write_cache()`
**描述**：chat/code 路由的语义缓存从永不过期改为 24h TTL（防缓存中毒）。这是有意为之的 write-through 策略，但对依赖永久缓存的场景是**静默的行为变更**。
**影响**：24h 后缓存过期，下次相同问题需要重新调用 LLM。对话成本略增，但消除了缓存中毒风险。
**建议**：如需恢复永久缓存，将 `24 * 3600` 改回 `None`。

---

## [遗留] PPT 创建时 JSON 解析频繁失败

**位置**：`tools/builtin/ppt_tool.py`
**描述**：模型生成的 PPT JSON 中，HTML 部分包含 SVG data URL（`url("data:image/svg+xml,%3Csvg...%3E")`），其中的 `%22`（编码的双引号）和特殊字符破坏 JSON 结构，导致 `json.loads` 失败。模型需要重试，浪费 30+ 秒。
**复现**：让模型创建带有渐变背景、波浪装饰等复杂视觉效果的 PPT。
**建议**：
1. 在 `ppt_tool.py` 中对 HTML 部分做预处理：先 base64 编码再嵌入 JSON，或用专门的 HTML 字段传递
2. 或在 system prompt 中指示模型不要在 PPT HTML 中使用 SVG data URL

---

## [遗留] `_store` 本地缓存 `get()` 是同步调用

**位置**：`memory/store.py` `get(conv_id)`
**描述**：`context_builder.build_messages()` 等多处通过 `memory_store.get(conv_id)` 同步读取本地缓存。Redis 失效通知到达后会刷新缓存，但刷新是异步的。在刷新完成前，同步 `get()` 可能拿到旧数据。
**影响**：极小——通常几十毫秒内刷新完成，且只影响跨 worker 场景。
**建议**：长期考虑将 `_store` 改为 Redis 直读（需要将同步调用方改为 async）。

---

## [遗留] `resume_stream` 跨 worker 轮询模式性能差

**位置**：`graph/runner/stream.py` `resume_stream()`
**描述**：当对话不在当前 worker 的 `_active_sessions` 中时，降级为每 0.5 秒轮询 DB event_log，最长 5 分钟。Redis `chatflow:streaming:{conv_id}` 可以帮助判断是否还在生成中，但当前 `resume_stream` 没有用这个信号。
**影响**：跨 worker 断线重连时体验差（轮询延迟 0.5s，且占 DB 资源）。
**建议**：`resume_stream` 检查 Redis `is_streaming()` 判断是否需要轮询，不在生成中则立即返回已有事件。

---

## [遗留] Reflector LLM 评估路径缺少 `forget_mode: False`

**位置**：`graph/nodes/reflector_node.py` `_llm_evaluate()`
**描述**：快速路径的 retry 已加 `forget_mode: False`，但 LLM 评估路径（边缘场景，约 10% 概率触发）返回 retry 时没有重置 forget_mode。
**影响**：极小——LLM 评估路径触发概率低，且 forget_mode 通常在该轮已经是 False。
**建议**：在 `_llm_evaluate` 的 retry 返回中也加上 `"forget_mode": False`。

---

## [遗留] 多工具并行执行时 `_pending_tool_exec_id` 只记录最后一个

**位置**：`graph/runner/stream.py` `_track_sse_for_db()`
**描述**：`_pending_tool_exec_id` 是单个 int，当 LLM 一次返回多个 tool_calls 时（如同时调用 web_search + execute_code），LangGraph ToolNode 并行执行。多个 `tool_call` SSE 事件快速到达，`_pending_tool_exec_id` 被后到的覆盖，前一个工具的 DB 记录找不到对应的 `tool_result` 来完成。
**影响**：并行工具调用时，先到的工具的 `tool_executions` 记录可能卡在 `running` 状态。
**建议**：将 `_pending_tool_exec_id` 改为 `dict[str, int]`（tool_name → exec_id），或用 tool_call_id 作为 key。

---

## [已修复] 本次修复清单

以下 bug 在本轮代码审查中发现并已修复：

| Bug | 修复 |
|-----|------|
| `_get_redis()` REDIS_URL 空值崩溃 | 加 `if not REDIS_URL: raise RuntimeError` |
| `_on_invalidate` 竞态 pop 导致 KeyError | 改为 `db_get_conversation` 刷新覆盖 |
| `onToolCallArgs` 找不到 `_generating` 标记的工具 | 加兜底 `findLast(t => !t.done)` |
| SSH 探活每次调用 +100ms | 加 `_pool_last_used`，30s 内跳过 |
| `tool_call_args` 每个 JSON 片段都发 SSE | 攒 200ms/500 字符一批 |
| 心跳 Redis 调用无超时阻塞 | `wait_for(timeout=2)` |
