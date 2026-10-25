# ChatFlow 后端测试用例文档

> 所有测试用例的设计文档。开发时只读此文件，不需要读测试代码。
> 测试代码在 `tests/` 目录，Docker 镜像构建时排除。

---

## 运行方式

```bash
# 全部测试
pytest tests/ -v

# 单个模块
pytest tests/test_fsm.py -v
pytest tests/test_db.py -v

# 按标记
pytest tests/ -m unit          # 纯单元测试（无 DB/Redis）
pytest tests/ -m integration   # 集成测试（需 DB/Redis）
pytest tests/ -m smoke         # 冒烟拨测（端到端）
```

---

## 一、状态机 (fsm/)

### T-FSM-01: 对话状态机合法转换

| 用例 | 输入 | 期望 |
|------|------|------|
| active → streaming | `send_event("streaming")` | current_value = "streaming" |
| streaming → completed | `send_event("completed")` | current_value = "completed" |
| streaming → error | `send_event("error")` | current_value = "error" |
| streaming → active (停止) | `send_event("active")` | current_value = "active" |
| completed → streaming (新一轮) | `send_event("streaming")` | current_value = "streaming" |
| error → streaming (重试) | `send_event("streaming")` | current_value = "streaming" |
| 全路径链: active→streaming→completed→streaming→error→streaming→active | 依次 send_event | 每步正确 |

### T-FSM-02: 对话状态机非法转换

| 用例 | 输入 | 期望 |
|------|------|------|
| active → completed (跳过 streaming) | `send_event("completed")` | 返回 "active"（不变），日志 warning |
| active → error | `send_event("error")` | 返回 "active"（不变） |
| completed → error | `send_event("error")` | 返回 "completed"（不变） |

### T-FSM-03: from_db_status 恢复

| 用例 | 输入 | 期望 |
|------|------|------|
| 有效状态 | `from_db_status("streaming")` | current_value = "streaming" |
| 无效状态 | `from_db_status("unknown")` | 降级到 "active" |
| 空字符串 | `from_db_status("")` | 降级到 "active" |

### T-FSM-04: 工具执行状态机

| 用例 | 输入 | 期望 |
|------|------|------|
| running → done | `send_event("done")` | current_value = "done" |
| running → error | `send_event("error")` | current_value = "error" |
| running → timeout | `send_event("timeout")` | current_value = "timeout" |
| done → error (终态不可转) | `send_event("error")` | 抛 TransitionNotAllowed |
| 无效事件 | `send_event("invalid")` | 返回 "running"（不变） |

### T-FSM-05: SSE 事件类型检测

| 用例 | 输入 | 期望 |
|------|------|------|
| 单 key | `{"content": "hi"}` | CONTENT |
| 多 key 优先级 | `{"tool_result": {}, "content": "x"}` | TOOL_RESULT（优先） |
| 控制事件最优先 | `{"done": True, "content": "x"}` | DONE |
| 空 dict | `{}` | UNKNOWN |
| thinking vs content | `{"thinking": "x", "content": "y"}` | THINKING（优先于 content） |

### T-FSM-06: 枚举 .value 兼容性

| 用例 | 期望 |
|------|------|
| `ToolExecutionStatus.RUNNING.value` | `"running"` |
| `ToolExecutionStatus.RUNNING == "running"` | `True` |
| `ConversationStatus("active") == ConversationStatus.ACTIVE` | `True` |
| `json.dumps({"s": ConversationStatus.ACTIVE})` | `'{"s": "active"}'` |

### T-FSM-07: 计划步骤状态机

| 用例 | 输入 | 期望 |
|------|------|------|
| pending → running | `send_event("running")` | current_value = "running" |
| running → done | `send_event("done")` | current_value = "done" |
| running → failed | `send_event("failed")` | current_value = "failed" |
| done → running (终态不可转) | `send_event("running")` | 抛 TransitionNotAllowed |
| pending → done (跳步) | `send_event("done")` | 抛 TransitionNotAllowed |
| from_db_status("running") | `PlanStepSM.from_db_status("running")` | current_value = "running" |
| from_db_status 无效值 | `PlanStepSM.from_db_status("xxx")` | current_value = "pending"（降级默认值） |
| PlanStepStatus 枚举 | `PlanStepStatus.DONE.value` | `"done"` |

---

## 二、数据库层 (db/)

### T-DB-01: tool_store CRUD

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 创建工具记录 | `create_tool_execution(conv_id, msg_id, "web_search", {})` | 返回 int > 0，status="running" | integration |
| 创建工具记录带 step_index | `create_tool_execution(..., step_index=1)` | DB 行 step_index=1 | integration |
| 创建工具记录无计划 | `create_tool_execution(..., step_index=None)` | DB 行 step_index=NULL | integration |
| 完成工具 | `complete_tool_execution(id, output="ok", status="done")` | DB 行 status="done" | integration |
| 带搜索结果完成 | `complete_tool_execution(id, search_items=[...])` | search_items 非空 | integration |
| 获取对话工具 | `get_tool_executions_for_conv(conv_id)` | 返回列表含 step_index 字段，按 id 排序 | integration |
| 获取消息工具 | `get_tool_executions_for_message(msg_id)` | 只返回该 message 的工具 | integration |

### T-DB-01b: plan_store 关联

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 创建计划带 message_id | `create_plan(..., message_id="abc123")` | DB 行 message_id="abc123" | integration |
| 获取计划返回 message_id | `get_latest_plan_for_conv(conv_id)` | 返回 dict 含 message_id | integration |

### T-DB-02: artifact_store

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 保存新产物 | `save_artifact(conv_id, "test.py", ...)` | 返回 dict 含 id > 0，source="generated" | integration |
| 同路径覆盖 | 两次 save 同 path | 第二次更新不新增 | integration |
| detect_language | `detect_language("main.py")` | "python" | unit |
| detect_language 无后缀 | `detect_language("Makefile")` | "text" | unit |
| detect_language .tar.gz | `detect_language("a.tar.gz")` | "archive" | unit |
| get_artifact_content PPT 解包 | 存入 PPT JSON | 返回含 slides_html | integration |
| get_artifact_content archive 解包 | 存入 archive JSON | 返回含 binary_b64，无 slides_html | integration |
| save_artifact 返回正确 id | INSERT 新记录 | id > 0（flush 后获取） | integration |
| save_artifact source=uploaded | `save_artifact(..., source="uploaded")` | DB 行 source="uploaded" | integration |
| meta_list 包含 source | `get_artifact_meta_list(conv_id)` | 返回 dict 含 source 字段 | integration |
| bind_artifacts_to_message | `bind_artifacts_to_message([id1,id2], conv_id, msg_id)` | 仅匹配 conv_id 且 source='uploaded' 的行被 UPDATE | integration |
| bind 跨 conv 拒绝 | bind 其他 conv 的 artifact id | 不影响其他 conv 的行 | integration |

### T-DB-03: redis_state

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| publish_stop + check_stop | 先 publish 再 check | True | integration |
| check_stop 无信号 | 直接 check | False | integration |
| stop TTL 过期 | publish 后等 61 秒 | False | integration |
| register + is_streaming | 注册后查询 | True | integration |
| unregister | 注销后查询 | False | integration |
| heartbeat 续期 | 注册 → 等 5 秒 → heartbeat → 等 5 秒 | 仍 True | integration |
| REDIS_URL 为空 | `_get_redis()` | RuntimeError | unit |

### T-DB-04: migrate 幂等性

| 用例 | 期望 | 标记 |
|------|------|------|
| 连续执行两次 `run_migrations` | 第二次无报错 | integration |
| 新表不存在时创建 | tool_executions 表存在 | integration |
| 新列不存在时添加 | sandbox_worker_id 列存在 | integration |

---

## 三、记忆层 (memory/)

### T-MEM-01: store 消息操作

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| add_message INSERT | update_db_id=0 | DB 有新行，缓存有新 Message | integration |
| add_message UPDATE | update_db_id>0 | DB 行更新，缓存追加 | integration |
| add_message 带 tool_summary | 传入非空 | DB tool_summary 字段有值 | integration |
| add_message conv 不存在 | conv_id 无效 | 日志 warning，不崩溃 | integration |
| update_status | 设置 "streaming" | DB status 更新 + Redis 通知 | integration |
| create_message_immediate | 创建 user 消息 | 返回 id > 0，不更新内存缓存 | integration |

### T-MEM-02: compressor

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 新数据压缩 | msg.tool_summary 非空 | 压缩后 tool_summary 清空 | integration |
| 旧数据压缩 | content 含【工具调用记录】 | content 替换为 [old tools call] | integration |
| 不需压缩 | 消息数 < 阈值 | 返回 False | unit |

### T-MEM-03: context_builder

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 正常构建 | conv 有 5 条消息 | 返回 SystemMessage + 5 条 | unit |
| tool_summary 注入 | msg.tool_summary 非空 | AIMessage content 含 [工具调用摘要] | unit |
| 截断长回复 | content 1000 字 | 截断到 800 + "..." | unit |
| COMPAT 旧摘要截断 | content 含【执行过程摘要】 | 只保留摘要前的核心内容 | unit |
| forget_mode | forget_mode=True | 窗口缩短，不注入远期记忆 | unit |
| 长期记忆去重 | 记忆与近期消息重叠 >55% | 被过滤 | unit |

### T-MEM-04: 事实抽取 (rag/extractor.py)

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 抽取过滤低置信度 | LLM 返回 confidence=0.3 条目 | 不写入 | unit |
| 抽取截断过长 fact | fact 字段 400 字 | 截断到 MAX_FACT_LEN | unit |
| 非法 type 降级 knowledge | type="random" | type=knowledge | unit |
| 无 user/assistant 直接返回 | 任一为空 | 返回 [] | unit |
| LLM 返回非 JSON | raw="对不起..." | 返回 [] 并 warning | unit |
| ```json 围栏容错 | raw 首尾有 ```json | 解析成功 | unit |
| core_memory 渲染 | user_profile=["前端工程师"] | prompt 含 [身份] 前端工程师 | unit |
| 最多 MAX_FACTS_PER_TURN 条 | LLM 返回 12 条 | 只保留前 N 条 | unit |

### T-MEM-05: 事实更新决策器 (rag/updater.py)

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 无候选直接 ADD | search 返回空 | Decision.ADD | unit |
| 最高分低于阈值 ADD | max_score=0.5 (阈值 0.80) | Decision.ADD | unit |
| 全部候选已 superseded 仍 ADD | live_candidates=[] | Decision.ADD | unit |
| 仲裁 UPDATE 先写新后标记旧 | LLM 返回 UPDATE + target_id | upsert_fact → mark_superseded，顺序不可逆 | unit |
| target_id 不在候选列表 降级 ADD | LLM 返回 target_id=999 | Decision.ADD + warning | unit |
| 非法 decision 降级 ADD | decision="WTF" | Decision.ADD + warning | unit |
| LLM 调用失败 降级 ADD | llm.ainvoke 抛异常 | Decision.ADD | unit |
| 空 fact 直接 NONE | fact="" | Decision.NONE | unit |
| DELETE 决策 删除目标 | LLM 返回 DELETE | delete_point 被调用 | unit |
| UPDATE 过程失败 不留记忆空洞 | mark_superseded 失败 | 旧 fact 仍可检索（不静默丢失） | unit |

---

## 四、图节点 (graph/nodes/)

### T-NODE-01: reflector 五条快速路径

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| 路径1: 无计划 | plan=[] | done | unit |
| 路径2: 超边界 | current_idx >= len(plan) | done | unit |
| 路径3: 末步+有响应 | is_last=True, full_response非空 | done | unit |
| 路径3: 末步+工具失败 | is_last=True, last_tool_failed=True | retry | unit |
| 路径4: 中间步+有响应+首次 | is_last=False, step_iters=0 | continue | unit |
| 路径5: 无响应+可重试 | full_response="", iters<2 | retry, forget_mode=False | unit |
| 边缘: LLM 评估 | 重试中有响应 | 调用 LLM 评估 | integration |

### T-NODE-02: reflector 数据完整性

| 用例 | 期望 | 标记 |
|------|------|------|
| step_results 截断 | 每条 ≤ 3000 字符 | unit |
| _STEP_RESULT_SUMMARY_LEN | 步骤摘要 ≤ 2000 字符 | unit |
| retry 返回 forget_mode=False | 检查返回 dict | unit |

### T-NODE-03: planner

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| search 路由触发规划 | route="search" | plan 非空 | unit |
| chat 路由不规划 | route="chat" | plan=[] | unit |
| code 短消息不规划 | route="code", msg 50字 | plan=[] | unit |
| code 长消息规划 | route="code", msg 200字 | plan 非空 | unit |
| JSON mode 首次 | 第一次尝试 | 传 response_format | unit |
| JSON 解析失败兜底 | 响应非 JSON | 单步兜底计划 | unit |
| _fix_json_inner_quotes | `{"k":"他说"百业""}` | 内部引号替换 | unit |
| 续写检测 | user_msg="继续" | 尝试从 DB 恢复 | integration |

### T-NODE-04: call_model 澄清预检

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| 网页请求无风格 | "帮我做个网页" | needs_clarification=True | unit |
| 网页请求有风格 | "帮我做个简约风格网页" | 不触发（跳过） | unit |
| 非网页请求 | "帮我写代码" | 不触发 | unit |
| planner 已设标记 | needs_clarification=True(from planner) | call_model 跳过 | unit |
| current_step_index 越界保护 | idx=10, plan 长度 5 | 走 else 分支，不 IndexError | unit |

### T-NODE-05: call_model_after_tool

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| 工具失败检测 (DB) | tool_executions 最后一条 status=error | last_tool_failed=True | integration |
| 工具失败检测 (降级) | DB 查询异常 | 降级到文本匹配 | unit |
| 消息不截断 | messages 30 条 | 全部保留（不截断） | unit |

### T-NODE-06: save_response

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| tool_summary 独立存储 | 有工具调用 | content 不含【工具调用记录】 | unit |
| 缓存跳过工具响应 | tool_events 非空 | 不写缓存 | unit |
| 缓存 TTL | chat 路由 | TTL=24h | unit |
| 澄清 DB-first | state.clarification_data 有值 | 发 clarification 事件 | unit |
| 澄清工具路径 | request_clarification 已写入 contextvar 槽 | save_response 从槽读 data → 发 clarification 事件，并消费清空槽 | unit |
| 澄清槽 Task 隔离 | 在子 Task 中对 slot dict 做 in-place 变更 | 父 Task（save_response）读到变更后的值（验证共享引用语义） | unit |

### T-NODE-07: extract_memory (fire-and-forget)

| 用例 | 条件 | 期望 | 标记 |
|------|------|------|------|
| 关闭抽取 | FACT_EXTRACTION_ENABLED=False | 直接返回 {} | unit |
| 澄清轮跳过 | needs_clarification=True | 直接返回 {} | unit |
| 空 user/response 跳过 | user_msg="" 或 full_response="" | 直接返回 {} | unit |
| 后台异常不传播 | extractor 抛 RuntimeError | 节点 execute 仍返回 {}，logger.warning | unit |
| Task 强引用保留 | 并发 100 轮调用 | _pending 中的 Task 不被 GC 提前回收 | unit |
| conv 不在缓存 | memory_store.get(conv_id) is None | 静默跳过，不抛 | unit |

### T-TOOL-01: request_clarification (tools/builtin/clarification.py)

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 合法输入 | 1 个 single_choice 3 选项 | slot["data"] 含 question+items | unit |
| 选项不足降级 | options=["A"] | 该项被过滤，若没有合法项则返回警告字符串 | unit |
| 非法 type 过滤 | type="dropdown" | 该项被过滤 | unit |
| 超长 question 截断 | 500 字 | 截断到 200 字 | unit |
| items 超过 4 个截断 | 6 个合法项 | 只保留前 4 | unit |
| 空 question 拒绝 | question="" | 返回 ⚠️ 字符串，不写入槽 | unit |
| 未初始化 slot 兜底 | contextvar 为 None（非 HTTP 路径） | 返回警告，不抛 | unit |

---

## 五、流式处理 (graph/runner/)

### T-STREAM-01: 工具执行追踪（多工具并行）

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 单工具完整链路 | tool_call → sandbox_output → tool_result | tool_exec_map 创建→累积→完成 | unit |
| 多工具并行 | 3 个 tool_call 快速到达 | 3 条 tool_execution 记录，各自 seq 独立 | unit |
| tool_result 匹配正确 seq | 第 3 个工具完成 | 只完成 seq=3，seq=1,2 不受影响 | unit |
| sandbox_output 累积到正确工具 | 输出属于 current_tool_seq | 累积到对应 map entry | unit |

### T-STREAM-02: 消息终态化

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 正常终态化 | _finalize_message | stream_completed=True, buffer="" | unit |
| 重复终态化 | 调用两次 | 第二次跳过（_finalized=True） | unit |
| flush 和 finalize 并发 | 同时调用 | _finalize_lock 防竞态 | unit |
| event_log 不受 finalized 限制 | finalize 后 emit done 事件 | event_log 仍写入 | unit |
| 消息预写失败 | create_message_immediate 异常 | yield 错误 SSE → 中止 | unit |

### T-STREAM-03: SSE 事件检测

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| _detect_event_type 正常 | `data: {"content":"x"}` | "content" | unit |
| _detect_event_type 非法 | `not_data: xxx` | "unknown" | unit |
| ping 不写 event_log | emit ping 事件 | _event_batch 不增加 | unit |

### T-STREAM-04: 格式化器


| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| exit_code 提取正常 | `"⏱ 1.5s \| exit=0"` | 0 | unit |
| exit_code 提取失败 | `"⏱ 1.5s \| exit=1"` | 1 | unit |
| exit_code 负数 | `"⏱ 0.5s \| exit=-1"` | -1 | unit |
| exit_code 无匹配 | `"无结果"` | 0（默认） | unit |
| status 判定 | exit=0 → done, exit≠0 → error | 正确 | unit |

### T-STREAM-05: 持久化流恢复与停止（`tests/test_chat_recovery.py`）

运行：`python tests/run_regression.py -q tests/test_chat_recovery.py`（独立假配置，无外部服务）。

| 用例 | 期望 |
|------|------|
| 提交后发布 / 临时 DB 错误重试 | 未提交事件不发给客户端；失败批次保留，成功后按 ID 原序发布；每条带 `id` 和 `message_id`，ping 不落库 |
| DB 回放过程中有新提交 / 跨 worker | 全程按 `message_id` 和持久游标读分页，无内存尾部切换缺口，不混入旧轮终止事件 |
| 游标已超过终态 | 根据消息和对话 DB 状态及时关闭，不空等五分钟 |
| 等待 LLM 时点击停止 | 取消 graph，等待 consumer 保存部分消息及终态后返回 `stop_confirmed` |
| 终态发送顺序 | DB 状态和流租约清理完成后，才向原连接交付终止事件 |
| 初始化失败 / 首 token 前失败 | 错误传给 consumer，assistant 行标记完成，会话进入 FSM error |
| 两个同名工具交错输出、先完成第一个 | 使用 `tool_run_id` 关联，输出及终态不会串写；停止时其余 running 工具转 error |
| 匿名会话恢复 / 越权访问 | DB 返回 `client_id`；只有匹配的匿名 client 或 user 可以访问 |
| 进程退出后恢复 | 本 worker 无任务且 Redis 租约确认消失，才保留部分内容并关闭孤儿消息；Redis 不可用不判死亡 |
| 孤儿检查与新轮并发 | 行锁内校验 `expected_updated_at`，旧观察值不得关闭新轮 |
| 无新 token / 落库期间又来 token | 空闲不重复写大 buffer；版本号保证并发到达的 token 下一批仍会持久化 |

---

## 六、沙箱 (sandbox/)

### T-SANDBOX-01: 会话亲和 + DB 持久化

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 首次分配 | 新 conv_id | 负载均衡选 worker → DB 写入 sandbox_worker_id | integration |
| 缓存命中 | 同 conv_id 再次调用 | 直接从本地缓存返回（不查 DB） | integration |
| 跨 worker 恢复 | 本地无缓存，DB 有记录 | 从 DB 读取 worker_id | integration |
| 故障转移 | 缓存 worker 不健康 | 迁移到新 worker → DB 更新 | integration |

### T-SANDBOX-02: SSH 连接池

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 30s 内复用 | 两次调用间隔 < 30s | 跳过探活，直接返回 | unit |
| 30s 后探活 | 间隔 > 30s | echo ok 探活成功后返回 | integration |
| 探活失败重建 | echo ok 失败 | 重建 SSH 连接 | integration |

### T-SANDBOX-03: sandbox_download 安全

| 用例 | path 输入 | 期望 | 标记 |
|------|----------|------|------|
| 正常路径 | "." | 打包成功 | integration |
| 子目录 | "src" | 打包 src 目录 | integration |
| 路径遍历攻击 | "../../etc/passwd" | .. 被清理，不逃逸 | unit |
| 特殊字符 | "my file (1).txt" | shlex.quote 保护 | unit |
| 绝对路径 | "/etc/passwd" | 前导 / 被去除 | unit |
| 超大文件 | 60MB 文件 | 返回"超过 50MB 限制" | integration |

### T-SANDBOX-04: write_binary (SFTP)

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 写小二进制 | `worker.write_binary(path, b"\\x00\\x01\\xff")` | 远端文件字节完全相同 | integration |
| 写大二进制 (10MB) | 随机 10MB | stat 大小匹配，md5 一致 | integration |
| 父目录不存在自动创建 | path=`/tmp/a/b/c.bin` | 自动 mkdir -p，写入成功 | integration |
| upload_binary 去重 | 同 conv 同名上传两次 | 第二次自动后缀 `(1)` | integration |
| upload_binary 路径越权 | filename=`../etc/passwd` | basename 提取，不逃出 uploads/ | unit |
| upload_binary 超限 | 60MB 文件 | 抛 ValueError "超过…限制" | unit |

---

## 六-B、文件上传 (routers/files_router.py)

### T-UPLOAD-01: 用户文件上传端点

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 正常上传 | `POST /api/files/upload` form: conv_id + file | 200，返回 `{id,name,path,size,language,source:"uploaded"}` | integration |
| conv 不存在 | conv_id 未在 DB | 404 | integration |
| 文件过大 | 60MB | 413 (Request Entity Too Large) | integration |
| 空文件名 | filename="" | 400 | unit |
| 中文文件名 | "测试报告.pdf" | 写入沙箱成功，DB name 是 UTF-8 | integration |
| 同名上传 | 同 conv 同名两次 | 第二次返回 path 含 `(1)` | integration |
| 沙箱不可用 | 所有 worker 不健康 | 503 | integration |
| message_id 初始为空 | 上传后查 DB | message_id="", source="uploaded" | integration |

### T-UPLOAD-02: ChatRequest.file_ids 绑定与注入

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 发送带 file_ids | 上传两个文件 → POST chat with `file_ids=[id1,id2]` | 用户消息持久化后，两 artifact.message_id=user_msg.id | integration |
| 跨 conv file_ids | file_ids 含别的 conv 的 id | 不绑定（bind 函数过滤 conv_id） | integration |
| 上传清单注入 HumanMessage | file_ids 非空 | LLM 接收的消息体包含 `[用户上传…]` 前缀，列出文件名+路径 | integration |
| file_ids 为空 | 空列表 | 不注入清单，行为与原版一致 | unit |
| 上传后无消息 | 上传不发送 | artifact 留 message_id="" 直到 session 过期清理 | integration |

### T-UPLOAD-03: 端到端 smoke

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 拖入 zip → 模型解压 | 上传 archive.zip → 提示"解压看一下" | 模型 run_shell `unzip` 成功 | smoke |
| 刷新后看到上传卡片 | 发送后 F5 | full-state 返回 artifacts 含 source="uploaded" 项，前端在 user 消息旁渲染 | smoke |
| 图片+文件混合发送 | pendingImages + pendingFiles 同时非空 | images 走多模态路径，files 走沙箱路径，互不干扰 | smoke |

---

## 七、LLM 客户端 (llm/)

### T-LLM-01: astream_with_tools 节流

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 小参数不节流 | 100 字符参数 | 直接发送（< 500 阈值） | unit |
| 大参数节流 | 5000 字符参数 | 分批发送（200ms 或 500 字符） | unit |
| 流结束刷余 | 缓冲区有剩余 | yield 剩余内容 | unit |
| tool_call_start 通知 | 首次检测到工具名 | yield ("tool_call_start", name) | unit |

---

## 八、路由 (graph/edges.py)

### T-EDGE-01: should_continue

| 用例 | state | 期望 | 标记 |
|------|-------|------|------|
| 有 tool_calls | messages[-1] 有 tool_calls | "tools" | unit |
| 无 tool_calls + 有 plan | plan 非空 | "reflector" | unit |
| 无 tool_calls + 无 plan | plan=[] | "save_response" | unit |
| messages 为空 + 无 plan | messages=[] | "save_response" | unit |

### T-EDGE-02: should_continue_after_tool 工具限制

| 用例 | state | 期望 | 标记 |
|------|-------|------|------|
| 工具数 < 6 | 3 个 ToolMessage | "tools"（继续） | unit |
| 工具数 = 6 | 6 个 ToolMessage | "reflector"（强制） | unit |
| 无 plan 不限制 | plan=[], 10 个 ToolMessage | "save_response" | unit |

---

## 九、冒烟拨测 (端到端)

### T-SMOKE-01: 健康检查

| 用例 | 请求 | 期望 | 标记 |
|------|------|------|------|
| 后端存活 | `GET /api/tools` | 200, 返回工具列表 | smoke |
| 对话列表 | `GET /api/conversations` | 200, 返回数组 | smoke |

### T-SMOKE-02: 对话生命周期

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 创建 → 发消息 → 完成 | POST create → POST chat → 等 done | 消息存入 DB | smoke |
| 创建 → 删除 | POST create → DELETE | conversations 表无记录 | smoke |

### T-SMOKE-03: full-state 恢复

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| 刷新后恢复 | 发消息 → full-state | 消息+工具+产物完整 | smoke |
| tool_executions 关联 | 发含工具消息 → full-state | message 下有 tool_executions | smoke |
| artifacts 关联 | sandbox_write → full-state | message 下有 artifacts | smoke |

---

## 十、量化模块 (quant/)

> 设计来自 `docs/quant-trading-design.md`。M1 范围只覆盖后端选股 pipeline，不包含 LangGraph 和对话续接。

### T-QUANT-01: 选股 pipeline 端到端（FakeProvider）

> 已实现：`tests/test_quant_screen.py::test_screen_pipeline_end_to_end`

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| 默认条件全市场 | `ScreenCriteria(top_n=10)` | rows 按 total 降序、rank 从 1 起、universe_size 正确 | unit |
| provider_trace 完整 | 同上 | trace 至少含 `realtime_snapshot` + `daily_bars`，每条带 `elapsed_ms`/`rows` | unit |
| 因子原始值落到 raw | 同上 | 每行 `raw` 含 pe/pb/momentum/volatility 等键，缺失为 None | unit |
| 默认风险过滤 | exclude_st=true, exclude_suspended=true | rows 不含 ST 名称、不含 volume==0 标的 | unit |
| 综合分按权重合成 | 自定义 weights | total ∝ Σ(category_z × weight)/Σ(weight) | unit |

### T-QUANT-02: 股票池

> 已实现 hs300 用例：`test_universe_index_constituents`

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| universe=hs300 | `universe="hs300"` | 调用 INDEX_WEIGHT capability，universe_size = 成分股数 | unit |
| universe=custom 提供 symbols | `custom_symbols=["000001.SZ"]` | 仅命中标的进入候选 | unit |
| universe=custom 但 symbols 为空 | `custom_symbols=[]` | 回退到 all，warnings 包含说明 | unit |
| 指数 provider 失败 | provider 抛错 | 回退到全市场，warnings 含错误说明，logger.warning 有记录 | unit |

### T-QUANT-03: 硬过滤

> 已实现 pe_range：`test_hard_filters_pe_range`

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| pe_range | `pe_range=(5, 20)` | PE 超出区间标的剔除 | unit |
| pb_range | `pb_range=(0.5, 3)` | PB 超出区间剔除 | unit |
| min_market_cap（亿元） | `min_market_cap=500` | 总市值 < 500 亿剔除 | unit |
| min_avg_turnover（亿元） | `min_avg_turnover=5` | 成交额 < 5 亿剔除 | unit |
| 多重叠加 | pe + pb + market_cap 同时设 | 全部条件 AND 后剩余正确 | unit |

### T-QUANT-04: 因子计算与打分

> 已实现 zscore 空 Series：`test_factor_z_score_handles_empty`

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| 动量 | 70 日上涨趋势 close | momentum > 0 | unit |
| 波动率 | 高 jitter 序列 vs 平稳序列 | volatility 显著更大 | unit |
| MA 偏离 | close >> MA20 | ma_deviation 为正 | unit |
| zscore 空 Series | `pd.Series([])` | 返回空，不抛错 | unit |
| zscore 全 NaN | `pd.Series([NaN]*5)` | 返回全 NaN | unit |
| 负 PE 不参与基本面打分 | pe = -10 | fund_z 不被拉低；改由 risk_z 扣分 | unit |
| ST/停牌通过 risk_z 扣分 | is_st=true | risk_z 显著为负 | unit |

### T-QUANT-05: provider registry fallback

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| 单 provider 成功 | 1 个 OK provider | trace.status="ok"，rows 正确 | unit |
| 第一个失败 fallback | provider1 抛错，provider2 OK | trace 含 error+ok 两条；最终成功 | unit |
| 全部失败 | 所有 provider 抛错 | 抛 NoProviderAvailable | unit |
| capability 不匹配 | provider 不声明目标 capability | candidates 为空 → NoProviderAvailable | unit |
| health=DOWN 跳过 | provider 健康为 DOWN | candidates 不含它 | unit |

### T-QUANT-06: REST API

| 用例 | 请求 | 期望 | 标记 |
|------|------|------|------|
| QUANT_ENABLED=false | `GET /api/quant/providers` | 503 + "量化模块未启用" | smoke |
| 探活 | `POST /api/quant/providers/refresh` | 200，registry health 被刷新 | smoke |
| 选股请求 | `POST /api/quant/screen` body=ScreenCriteria | 200，结构化 ScreenResult | smoke |
| 参数错误 | top_n 类型不对 | 422 | smoke |
| 全 provider down | service 抛 NoProviderAvailable | 路由转 503 | unit |

### T-QUANT-ANALYZE: 分析 thinking 透传（spec §模型思考流程）

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-QUANT-ANALYZE-01 | FakeLLM 推 reasoning + content；`stream_analyze` 消费 | 输出包含 `thinking`/`delta`/`done` 三类事件；done 携带 `analysis`/`risk_notes`/`thinking_segments`（segment.node="quant_analyze", step_index=null, phase∈{reasoning,content}） | unit |
| T-QUANT-ANALYZE-02 | rows=[]，stream_analyze 短路 | 仅 1 个 done 事件，文案"未筛选到任何符合条件的股票。"，LLM 不被调用 | unit |

### T-FINANCE-DATA: 财经数据层（quant/data/）

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-FINANCE-DATA-01 | `cache.get_or_fetch` 同 key 调两次 | fetcher 只执行 1 次（第二次命中） | unit |
| T-FINANCE-DATA-02 | Redis `get`/`set` 全部抛错 | 不阻断主流程，每次直连 fetcher，返回值正常 | unit |
| T-FINANCE-DATA-03 | news 归一化：列名映射 + 空标题过滤 | 中文列名转英文 key，空 title 行被过滤 | unit |
| T-FINANCE-DATA-04 | money_flow 归一化：取最后一行为 today，history ≤5 行 | today.date 是最新日期，main_net_inflow 数值正确 | unit |
| T-FINANCE-DATA-05 | industry 归一化：按涨跌幅降序 + top_n 截断 | 输出顺序按 change_pct 从大到小，长度 ≤ top_n | unit |

### T-FINANCE-TOOL: finance 工具按 route 隔离

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-FINANCE-TOOL-01 | discover finance 子包后查询 7 个工具 tag | 全部带 `"finance"` tag | unit |
| T-FINANCE-TOOL-02 | `filter_tools_by_route(tools, "chat")` | 7 个 finance 工具被剔除；calculator/web_search 仍存在 | unit |
| T-FINANCE-TOOL-03 | `filter_tools_by_route(tools, "finance")` | 7 个 finance 工具全部保留 | unit |

### T-FINANCE-ROUTE: finance 路由 + 路由专用 system prompt

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-FINANCE-ROUTE-01 | 检查 `_ROUTE_CANDIDATES` | 含 `finance`；`search_code` 在 `search` 之前（防止部分匹配） | unit |
| T-FINANCE-ROUTE-02 | `build_messages(conv, route="finance")` | SystemMessage 含"金融分析模式"+"不构成投资建议" | unit |
| T-FINANCE-ROUTE-03 | `build_messages(conv, route="chat")` | SystemMessage 不含 finance prompt | unit |

---

## 十-B、股票预测 - 资讯搜索 (graph/prediction/news_search.py)

> 测试文件：`tests/test_prediction_news.py`。运行：`pytest tests/test_prediction_news.py -v -m unit`
> 设计来自 `graph/prediction/news_search.py`（全量不截取 + per-prediction 预算 + 12 维编排）。
> 隔离策略：httpx.AsyncClient / _incr_prediction_count / get_chat_llm / asyncio.sleep 全部 monkeypatch；
> news_search import chain 不依赖 statsmodels/arch，无需 sys.modules 占位。

### T-PRED-NEWS: 资讯搜索 helper

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-NEWS-01 | `_normalize_result(raw_content=20000字符)` | `full_content` 长度 == 20000（不切 4500），无 `chunks` 字段 | unit |
| T-PRED-NEWS-02 | httpx post 持续抛 ConnectError | post 调用次数 == `TAVILY_RETRY_TIMES`，结果 note 含"连接失败"，results=[] | unit |
| T-PRED-NEWS-02b | 正常 post 返回固定 payload | answer 正确，full_content 保留完整 20000 | unit |
| T-PRED-NEWS-03 | fake LLM astream 吐 JSON 数组 `["q1","q2"]`；`generate_queries` 消费 | 解析出 1-2 条 query，content token 经 on_delta 转发 | unit |
| T-PRED-NEWS-03b | LLM 吐带 _THINK_PREFIX 推理段 + content | thinking 剥离前缀后转发，content 正常解析为 `["q1"]` | unit |
| T-PRED-NEWS-03c | LLM 输出非 JSON | 兜底拼 `{symbol} {label}`，返回 1 条不空 | unit |
| T-PRED-NEWS-04 | fake LLM astream 吐 `{sentiment:0.6,reason:"看多"}`；`score_sentiment` 消费 | 返回 (0.6, "看多") | unit |
| T-PRED-NEWS-04b | sentiment=5.0 / 输出垃圾文本 | 5.0 钳制到 1.0；垃圾文本兜底 (0.0, "") | unit |
| T-PRED-NEWS-05 | mock tavily_search + get_chat_llm；`search_all_dimensions(symbol, market, pred)` 默认 12 维 | digest 长度 == len(NEWS_DIMENSIONS)；每维 on_dimension 回调触发；news_sentiment ∈ [-1,1]；每条含 dimension/query/answer/results/sentiment/sentiment_reason | unit |
| T-PRED-NEWS-05b | 传 dimensions 子集 2 维 | digest 长度 == 2，dimension 顺序保留 | unit |
| T-PRED-NEWS-06 | `_incr_prediction_count` 返回 `BUDGET+1` | tavily_search 返回 note "预算耗尽"、results=[]，httpx post 调用 0 次（预算短路在 httpx 之前） | unit |

---

## 十-C、股票预测状态机 (fsm/prediction.py)

> 测试文件：`tests/test_prediction_fsm.py`。运行：`pytest tests/test_prediction_fsm.py -v -m unit`
> 覆盖 PredictionSM 生命周期、非法转换兜底、DB 恢复、枚举值、卡住任务清理。
> 铁律对照：#1 状态从 DB 字段读、#6 状态机校验先于写库、#9 异常不吞。
> 隔离策略：fsm/prediction 仅依赖 python-statemachine（运行时 import，collection 不触发）；
> cleanup 测试 monkeypatch `AsyncSessionLocal`（假异步会话）+ owner lease，不碰真实 DB。

### T-PRED-FSM: PredictionSM 状态机

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-FSM-01 | queued→news_searching→inferring→done 依次 send_event | 每步 current_value 正确，done 终态 | unit |
| T-PRED-FSM-02 | queued/news_searching/inferring 各自 send_event("failed") | current_value = "failed" | unit |
| T-PRED-FSM-03a | queued→done（跳步）send_event | 返回 "queued"（不变），记 warning，不抛 | unit |
| T-PRED-FSM-03b | queued→inferring（漏 news_searching） | 返回 "queued"（不变） | unit |
| T-PRED-FSM-03c | done/failed 终态后试任意目标 | 返回当前值不变 | unit |
| T-PRED-FSM-03d | send_event("garbage") | 返回 "queued"（不变） | unit |
| T-PRED-FSM-04a | `from_db_status("inferring")` | current_value = "inferring"，可续 → done | unit |
| T-PRED-FSM-04b | `from_db_status("garbage")` / `from_db_status("")` | 降级到 "queued" | unit |
| T-PRED-FSM-05 | PredictionStatus 枚举 | 恰好 5 个值 {queued,news_searching,inferring,done,failed}；(str,Enum) 与字符串相等；`PredictionStatus("queued")` 构造；json.dumps 输出字符串 | unit |

### T-PRED-CLEAN: cleanup_stale_prediction_sessions（mock AsyncSessionLocal）

| 用例 | 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-CLEAN-01 | 无 owner 的旧运行任务 + 有 owner 的旧运行任务 | 只将无 owner 的任务经 FSM 改成 paused，保留续跑能力；默认至少 60 秒 grace | unit |
| T-PRED-CLEAN-02 | 候选查出后 updated_at 刷新 | 抢 lease 后行锁内复查 updated_at，跳过活跃任务；判断更新时间而非创建时间 | unit |
| T-PRED-CLEAN-03 | 没有候选 / Redis 不可用 | 返回 0，不猜测 owner 已死、不批量强写 failed | unit |

---

## 十-D、股票预测 - 鉴权 (prediction auth)

> 设计来自 `services/auth/dependencies.py::verify_admin_access` + `routers/admin_router.py` + `routers/prediction_router.py`。
> 铁律 #1：admin key 拦截来自后端 403，非前端 v-show。prediction 路由全量 admin 鉴权（D5）。
> 测试文件：`tests/test_prediction_auth.py`，运行：`pytest tests/test_prediction_auth.py -v -m unit`。
> 隔离（mirror test_quant_analyze/screen）：monkeypatch `config.ADMIN_SECRET_KEY` + `admin_router.ADMIN_SECRET_KEY` 到测试值；prediction store 函数（save_prediction_snapshot / list_prediction_records / get_active_prediction / get_prediction_snapshot）全 mock；statsmodels/arch 注入 sys.modules 桩（engine.py 延迟 import 的防回归保险，正常 collection 不触发）。

### T-PREDICT-AUTH-01: verify_admin_access 依赖

> 已实现：`test_verify_admin_access_*`

| 用例 | 输入 | 期望 | 标记 |
|------|------|------|------|
| 无 X-Admin-Key header | `verify_admin_access(x_admin_key=None)` | HTTPException 403 | unit |
| 错误 key | `verify_admin_access(x_admin_key="wrong")` | HTTPException 403 | unit |
| 正确 key | `verify_admin_access(x_admin_key=TEST_KEY)` (== config.ADMIN_SECRET_KEY) | 返回 True | unit |

### T-PREDICT-AUTH-02: POST /api/admin/verify

> 已实现：`test_admin_verify_*`

| 用例 | 请求 | 期望 | 标记 |
|------|------|------|------|
| 正确 key | `POST /api/admin/verify` body `{"key": TEST_KEY}` | 200，`{"ok": True}` | unit |
| 错误 key | body `{"key": "nope"}` | 401 | unit |

### T-PREDICT-AUTH-03: prediction 路由无 X-Admin-Key → 403

> 已实现：`test_prediction_*_no_key_403`。鉴权依赖先于业务逻辑执行。

| 用例 | 请求（无 X-Admin-Key） | 期望 | 标记 |
|------|------|------|------|
| GET /api/prediction/records | 无 header | 403 | unit |
| GET /api/prediction/active | 无 header | 403 | unit |
| GET /api/prediction/{id} | 无 header | 403 | unit |
| POST /api/prediction/predict | 合法 body、无 header | 403（鉴权先于 body 业务） | unit |

### T-PREDICT-AUTH-04: 正确 key + store mock → 200

> 已实现：`test_prediction_*_correct_key_200`。prediction_store 全 mock，不触 DB。

| 用例 | 请求 | 期望 | 标记 |
|------|------|------|------|
| records 列表 | GET /records + key | 200，`[]` | unit |
| active 查询 | GET /active + key（mock 返回 None） | 200，`{"active": False}` | unit |
| 创建预测 | POST /predict + key + 合法 body | 200，`status="queued"`、`prediction_id` 以 `pred_` 开头、`save_prediction_snapshot` 以 `status="queued"` 调用 | unit |

### T-PREDICT-AUTH-05: X-Client-ID → client_id（admin key-only 路径 D1）

> 已实现：`test_client_id_*`。验证 `_client_id_from_header`：取 X-Client-ID，无/空白降级 "admin"。

| 用例 | X-Client-ID header | store 收到 client_id | 标记 |
|------|------|------|------|
| 有 client id | `user42` | `user42` | unit |
| 无 header | （缺失） | `admin`（默认） | unit |
| 空白 | `"   "` | `admin`（strip 后空 → 降级） | unit |

---

## 十-E、股票预测 - 计算引擎 (graph/prediction/engine.py + factors/prediction_indicators.py)

> 测试文件：`tests/test_prediction_engine.py`。运行：`pytest tests/test_prediction_engine.py -v -m unit`
> 覆盖 `forecast()`（ARIMA+GARCH+蒙特卡洛+资讯修正）+ `compute_all()` 技术指标快照。
> 隔离（mirror test_quant_analyze/screen）：系统 python 无 statsmodels/arch，故在 `sys.modules` 层
> 注入 fake 模块——fake `ARIMA.fit().forecast(steps)` 返回固定步进漂移（last_close×1.002^(t+1)），
> fake `arch_model().fit().forecast()` 返回固定方差（→ sigma_annual=0.30），fake `fit().params`
> 返回 GARCH(1,1) 的 ω/α/β（α+β=0.98<1），让 forecast 走真实 ARIMA/GARCH 代码路径而非降级路径。
> 合成 bars_df（~60/90 行、上升 close）驱动；MC 用 `np.random.seed` 固定以消除随机噪声。
>
> ⚠ **踩过的坑（改测试前必读）**：fake 的安装必须**无条件覆盖** `sys.modules`，且用 autouse
> fixture 在每个用例前重装。`tests/test_prediction_auth.py`（按字母序更早 collect）在模块级也塞了
> 一份**哑桩**（`ARIMA = object`、`arch_model = lambda: None`）。原来这里写的是
> `if "statsmodels" not in sys.modules:` —— 哑桩先占坑，本文件的 fake 就被静默跳过：
> **单跑 test_prediction_engine.py 走真 ARIMA/GARCH，全套件跑却走 naive-drift + 历史 std 降级**，
> 同一批断言实际在测两套代码路径。老断言只校验区间（`0<=confidence<=1`）所以一直是绿的，
> 直到 T-PRED-V2-01 断言 `arima_aic != 0` 才暴露。
> 铁律对照：#2 结构化数据走独立 JSONB key（point_forecast/p10/p50/p90/inflection_points）、
> #4 async 纪律（forecast 是 async def，测试用 asyncio.run）、#9 不吞异常（引擎 except 均 warning+降级）。

### T-PRED-ENGINE: 计算引擎 forecast + 技术指标

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-01 | `forecast(bars_60, sentiment=0, horizon=5)` | 返回 dict 含 {dates,point_forecast,p10,p50,p90,inflection_points,confidence,model_name,technical_indicators}；dates/point_forecast/p10/p50/p90 长度均 == horizon；confidence ∈ [0,1]；model_name == `engine.MODEL_NAME`（v2 = "ARIMA+GARCH+Event+MC"；断言直接引用常量，改名不用再逐条改测试） | unit |
| T-PRED-02 | 同上 | 每个时点 p10 ≤ p50 ≤ p90，且全部有限值 | unit |
| T-PRED-03 | 同上 | point_forecast == p50（中位点预测） | unit |
| T-PRED-04 | 同上 | dates == last_date 之后 horizon 个工作日（bdate_range，跳周末） | unit |
| T-PRED-05 | 同上 | technical_indicators：rsi14 ∈ [0,100] 且 ≥50（上升）；macd 含 dif/dea/hist；boll upper≥mid≥lower；kdj 含 k/d/j 且 j 有限 | unit |
| T-PRED-06 | `_make_bars_reversal_up`（下行→急速反弹）+ `_make_bars_reversal_down`（上行→急速回落）直接喂 `kdj()` | 反弹序列 J>100、回落序列 J<0（验证 J 不被 clip 到 [0,100]） | unit |
| T-PRED-07 | `np.random.seed(2024)` 固定后 `forecast(bars, sentiment=+1)` vs `forecast(bars, sentiment=-1)` | sentiment=+1 的 point_forecast 均值 > sentiment=-1（资讯 ±15% 年化漂移修正方向；±1 同触发波动率扩大，sigma 相同，唯一差异是漂移） | unit |
| T-PRED-08 | `forecast(bars, sentiment=999.0)` 与 `sentiment=-999.0` | 被钳制到 [-1,1]，不抛异常，返回 5 长度 point_forecast | unit |
| T-PRED-09 | `forecast(pd.DataFrame(), 0, 5)`（空 df） | 返回默认 ForecastResult（model_name 占位、point_forecast=[]、confidence=0） | unit |
| T-PRED-10 | close 全 NaN 的 bars | 同降级默认 ForecastResult | unit |
| T-PRED-11 | `compute_all(bars_60)` | 含 {ma5,ma10,ma20,ema12,ema26,macd,rsi14,boll,kdj,last_close}；last_close == bars 末值；ma5/ma20 有限 | unit |
| T-PRED-12 | `compute_all(pd.DataFrame())`（空） | 全 NaN 指标快照，不抛异常 | unit |
| T-PRED-13 | `_inflection_points(dates,[10,11,12,11,10])` | 检出 peak，price≈12，date="d3" | unit |
| T-PRED-14 | `_naive_drift(s_20rows, horizon=5)`（<30 行降级路径，不依赖 statsmodels） | path 长度 5 且正漂移序列单调上升；ann 有限 | unit |
| T-PRED-15 | `_hist_std_vol(rets, 5)`（GARCH 降级路径） | sigma>0、var_path 长度 5 且全正 | unit |
| T-PRED-16 | `_monte_carlo(100, mu=0.10, sigma=0.30, 5, 500, (10,50,90))` | p10/p50/p90 长度=5 且 p10≤p50≤p90 | unit |

### T-PRED-V2: 引擎 v2（ModelParams / 事件分双通道 / 7 根 OHLC 蜡烛 / on_step 流式推导）

> v1 把 ARIMA order/AIC、GARCH ω/α/β 全丢在局部变量里（最多 log 一行），前端无从解释预测结果。
> v2 全链路贯穿：engine → `ForecastResult.model_params` → `prediction_snapshots.model_params` → 公式卡片。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-V2-01 | `forecast(bars_90, sentiment=0, horizon=7)` | `model_params` 真带回 ARIMA `arima_order`(p,d,q) + `arima_aic`(有限且≠0) + `arima_grid_tried`(每项含 order/aic)；GARCH `garch_omega`>0（且 == fake ω÷100²，百分数方差→小数方差）、`garch_alpha/beta` == fake 值、`garch_sigma_annual`≈0.30；MC 设定 `mc_n_paths`>0 / `mc_percentiles` / `mc_dt`>0；影线 `0 ≤ shadow_k_dn < 1`（≥1 会让预测蜡烛下影线跌破 0） | unit |
| T-PRED-V2-02 | 同上 | GARCH 平稳性：`garch_persistence == α+β` 且 **< 1**（≥1 = 波动无限累积，UI 需提示） | unit |
| **T-PRED-V2-03** | `filing_digest`（3 条爆雷 -0.9 + 2 条减持 ratio=0.02）vs `filing_digest=None` 基线 | ★ **事件分双通道**：F ∈ [-1,0) 且 `event_scores.negative/reduction` < 0；通道①漂移被压低 `drift_mu_annual_final` < 基线、`sentiment_total` < 基线；通道②波动被抬高 `vol_multiplier_event` > 1 且 `sigma_annual_final` > 基线（基线 M_event == 1.0）；GARCH/ARIMA 底座不变（差异只来自事件分）。**只压 μ 不抬 σ 会得出「稳稳地跌」的荒谬结论** | unit |
| T-PRED-V2-04 | `filing_digest=[]` | F == 0、`vol_multiplier_event` == 1.0、`event_scores` 三通道全 0（没有公告 ≠ 扣分） | unit |
| T-PRED-V2-05 | 维度 `failed=True` 但残留 `score=-0.9` | `filing_score` == 0.0 —— 取数失败整维跳过，**绝不把「没拿到数据」算成「没有坏消息」** | unit |
| T-PRED-V2-06 | `forecast(..., horizon=7)`（用户拍板 7 个交易日） | 恰好 7 根蜡烛；`day` == 1..7 连续；`date` 与 `forecast.dates[i]` 对齐；OHLC 不变式 `h ≥ max(o,c)`、`l ≤ min(o,c)`、`l > 0`；`p10 ≤ p50 ≤ p90`；`candle.p50 == forecast.p50[i]`；`point_forecast == p50` | unit |
| T-PRED-V2-07 | `forecast(..., on_step=cb)` | 事件类型只出 `SSEEventType.FORMULA_STEP/FORECAST_DAY`（铁律 #3 枚举）；**所有公式步骤都在第一根蜡烛之前**推完（否则前端卡片乱序弹出）；推导链恰为 `[technical, arima, garch, event_score, sentiment_total, gbm, shadow]`；蜡烛 day 严格 1..7；流式推出去的条数 == 最终 `formula_steps`/`forecast_candles` 落库条数 | unit |
| T-PRED-V2-08 | 同上，检查 payload | FORMULA_STEP 必须带 `stage`（== `key`）——`append_prediction_formula_step` 的去重键；SQL 里 `e->>'stage' IS DISTINCT FROM NULL`，**payload 缺这个字段会把之前所有公式步骤全删掉**。FORECAST_DAY 必须带 `date`（其去重键） | unit |
| T-PRED-V2-09 | `on_step` 回调抛 RuntimeError | 回调确被调用；forecast 仍返回完整 7 根蜡烛 + model_params（铁律 #9：回调是调用方代码，炸了只降级 warning，不能带崩整个预测） | unit |

---

## 十-F、股票预测 - 数据层护栏 (quant/data/announcement.py)

> 测试文件：`tests/test_prediction_data_layer.py`。运行：`pytest tests/test_prediction_data_layer.py -q`
> **一行真网络都不打**：`sys.modules` 注入假 akshare（只认 6 个真实接口名），`_em_report`(httpx) 桩掉，
> `AK_BACKOFF_BASE` 调 0 避免退避 sleep，每例清 `_TABLE_CACHE`（全市场表 30min TTL 缓存会串味）。
> 真实取数已在容器实测（000001.SZ notices=1733 / 300750.SZ negative=10 / **601288.SH 全 0 不崩**），
> 这里只做回归护栏。
>
> **头号坑**：akshare 查到 0 条时**不返回空表，而是抛 KeyError**
> （`stock_individual_notice_report` → `KeyError('代码')`，源码 stock_notice.py:118 未判空直接 assign 列名）。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-DATA-01 | `ak.stock_individual_notice_report` 抛 `KeyError('代码')` | `_ak_frame` 返回**空 DataFrame**（0 条不是故障） | unit |
| T-PRED-DATA-02 | 同上 | KeyError **不触发重试**（调用次数 == 1；重试 2 次 = 白等 3 秒还是 0 条） | unit |
| T-PRED-DATA-03 | `fetch_stock_notices("601288.SH")` + KeyError | 返回 `[]` 且不抛 | unit |
| T-PRED-DATA-04 | `fetch_negative_events` + 公告/业绩预告双 KeyError | 返回 `[]`（干净票 601288 的真实场景，不崩整个维度） | unit |
| T-PRED-DATA-05 | `fetch_placements` + `stock_qbzf_em` KeyError | 返回 `[]` | unit |
| T-PRED-DATA-06 | `fetch_reductions` + KeyError | 返回 `[]`（设计文档原写的 `symbol='减持'` 会直接 KeyError，正确值是 `'股东减持'`） | unit |
| **T-PRED-DATA-07** | ak 持续抛 `RuntimeError("HTTP 429")` | ★ **反面**：重试 `AK_RETRY_TIMES` 次后抛 `AnnouncementDataError`，**绝不降级成 []**。KeyError→[] 与 网络错→[] 只差一个 except 分支，语义却是「这只票没有减持」vs「我们不知道有没有」——降级会让 UI 对着限流显示「零风险」 | unit |
| T-PRED-DATA-07b | `fetch_stock_notices` 遇网络故障 | `AnnouncementDataError` 冒泡到 fetcher 之外（上游据此标维度 failed） | unit |
| T-PRED-DATA-07c | 一次抖动后重试成功 | 正常返回数据，调用 2 次（重试不是摆设） | unit |
| T-PRED-DATA-08 | 调 akshare 里不存在的接口名（版本漂移） | 抛 `AnnouncementDataError`，不静默返回空表 | unit |
| T-PRED-DATA-09 | 接口返回 `None` | 按空表处理（下游 `df.empty` 不会 AttributeError） | unit |
| T-PRED-DATA-10 | `classify_notice` / `title_hash` / `strip_highlight` | 治理类 → `is_noise=True`（规则层丢弃，0 次 LLM）；退市风险 → `needs_llm=False` + `severity<0`（结构化硬字段）；模糊标题 → `needs_llm=True`（进候选池）；★ 每条只贴**一个** `event_type`（三个风险维度候选池天然互斥，不重复判）；`title_hash` 稳定（缓存键）；`strip_highlight` 剥掉 `<em>` | unit |

---

## 十-G、股票预测 - 维度注册表 (graph/prediction/dimensions.py + domain.PredictionCriteria)

> 测试文件：`tests/test_prediction_dimensions.py`。运行：`pytest tests/test_prediction_dimensions.py -q`
> 注册表是前后端**唯一真相源**：前端 Picker 全靠 `GET /api/prediction/dimensions` 下发的
> label/desc/kind/markets 渲染（零文案硬编码），后端靠它决定跑哪些 fetcher。它一歪，两头一起歪。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-DIM-01 | `all_keys()` / `DIMENSION_GROUPS` | 恰好 **26** 维、key 唯一；分组**不重不漏**（覆盖全部 key 各一次——漏了的维度在前端面板里根本不出现，用户永远勾不到） | unit |
| T-PRED-DIM-01b | 遍历注册表 | 每维 label/desc 非空（前端直接显示这句话）、kind ∈ {search,data}、markets ⊆ {cn_a,us_stock}、`name` 是 `label` 的别名 | unit |
| T-PRED-DIM-02 | `data_keys()` / `search_keys()` | **6 data + 20 search**；data 维**必须**有 `fetcher`（否则维度必然 failed）；search 维**必须没有** fetcher（否则被 `_run_dimension` 误判成数据型）；`split_by_kind` 不吞 key、两类不相交 | unit |
| T-PRED-DIM-03 | `sec_filings` 的市场归属 | 只在 us_stock：`supports("cn_a")==False`、不在 `dimensions_for_market("cn_a")`、不在 `default_dimensions("cn_a")`；按市场过滤只做减法（默认集 ⊆ 该市场支持集） | unit |
| T-PRED-DIM-04 | `validate_dimensions(["macro_policy","sec_filings","not_a_real_dimension","macro_policy"], "cn_a")` | valid == `["macro_policy"]`（去重且保序）；invalid == {sec_filings, not_a_real_dimension}。**v1 完全不校验：垃圾 key 会照跑 Tavily 烧钱** | unit |
| T-PRED-DIM-05 | `PredictionCriteria(dimensions=["negative_event","sec_filings","bogus"], market="cn_a")` | 静默丢弃非法 key（单个坏 key 不该让整单预测 400），保留 `["negative_event"]`；**全非法** → 回退该市场默认集（而不是跑一张空图让用户对着空白页等 90 秒） | unit |
| T-PRED-DIM-05b | `PredictionCriteria(symbol, market)` 不传 dimensions | `horizon == 7`（用户拍板）；`resolved_dimensions()` == 该市场默认集 | unit |
| T-PRED-DIM-06 | `to_payload("us_stock")` | 字段恰为 `{key,label,desc,kind,markets,fetcher,default_on}`（与前端 `Dimension` 接口逐一对齐）；`markets` 是 **JSON 数组**（不是 tuple）；`default_on` 是 bool | unit |
| **T-PRED-DIM-07** | `gather_all_dimensions` 混跑 3 搜索维 + 2 数据维（桩掉 `ns.httpx.AsyncClient`，真 `tavily_search` 照跑） | ★ Tavily 调用次数 == 搜索维度数、预算计数 `_incr_prediction_count` 次数 == 搜索维度数 —— **数据型维度一次 Tavily 都不调、不占按次计费的检索额度**；两类结果各归各的桶（news_digest / filing_digest，后者 `kind=="data"`）。<br>⚠ 写这条时别桩掉 `tavily_search` 本体——预算计数器就长在它里面（先递增再发 HTTP），桩掉它断言永远是 0（假绿）。要桩就桩它下面的 httpx | unit |

---

## 十-H、股票预测 - v2 持久化 / 增量落库 / 删除 / 刷新重建 (db/prediction_store.py)

> 测试文件：`tests/test_prediction_store_v2.py`。运行：`pytest tests/test_prediction_store_v2.py -q`
> `AsyncSessionLocal` 换成假 session（记录每条 SQL 与参数），**无 Postgres**。断言的是**契约**
> （写哪一列、去重键是什么、删不删 event_log），不是 SQL 字符串长相。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-STORE-01 | `append_prediction_dimension(kind="search"/"data")` | search → `news_digest` 列、data → `filing_digest` 列（走错列前端永远读不到那批卡片）；去重键恒为 `dimension`；SQL 含 `jsonb_array_elements` + `IS DISTINCT FROM` 剔旧子查询（续跑重写同一维不会长出重复卡片） | unit |
| T-PRED-STORE-02 | `append_prediction_formula_step` / `_forecast_candle` / `_tool_trace` | 去重键分别为 `stage` / `date` / **无**。蜡烛按 date 去重否则刷新重放会画出 14 根；工具轨迹不去重（同一工具会被多个维度各调一次，每次都是独立轨迹） | unit |
| T-PRED-STORE-03 | `_append_jsonb_item(column="news_digest; DROP TABLE users--")` | 返回 False 且**一条 SQL 都不发**（列名会拼进 SQL → 只能走白名单，这是注入面） | unit |
| T-PRED-STORE-04 | 快照不存在（rowcount=0） | 返回 False，不抛 | unit |
| T-PRED-STORE-05 | session.execute 抛 RuntimeError | 返回 False + **不抛**（铁律 #9：增量落库失败不能阻塞图执行） | unit |
| **T-PRED-STORE-06** | `delete_prediction("pred_del")` | ★ 发**两条** DELETE：先 `DELETE FROM event_log WHERE conv_id=:pid`，再 `DELETE FROM prediction_snapshots`。**event_log 上零 FK 约束**（migrate.py 的 `CREATE TABLE IF NOT EXISTS` 因 create_all 先执行而永久 no-op，声明的 ON DELETE CASCADE 从未落地）→ 指望级联 = 每删一条预测泄漏几百行孤儿事件 | unit |
| T-PRED-STORE-07 | 快照不存在 | `delete_prediction` 返回 False（router 据此返回 404） | unit |
| **T-PRED-STORE-08** | `prediction_v2_fields(snap)` 读回 7 个 v2 列 | ★ `filing_digest / model_params / formula_steps / forecast_candles / event_markers / tool_traces / event_score` 一个不少、值与写入一致 —— **前端 `loadSnapshot()` 靠这些 DB 列重建整个 v2 界面（不靠 SSE 重放）**，少一列 → 硬刷新后对应区块直接空白 | unit |
| T-PRED-STORE-08b | 全新快照（列还是 NULL） | 数组列 → `[]`、model_params → `{}`（前端 v-for 不会炸）；`event_score` → **None**（没算出来时不能谎报 0 = 无风险） | unit |
| **T-PRED-STORE-09** | 驱动 `NewsSearchNode.execute`，fake gather 在**返回前**逐维回调 `on_dimension` | ★ **增量落库**：每维一跑完就 `append_prediction_dimension` 一次，并按 kind 分流（`[("macro_policy","search"), ("negative_event","data")]`）。**v1 是全跑完一次性写 → 一 cancel 库里一条都没有**，「暂停 = 取消 + 保留已完成维度 + 续跑剩余」无从谈起 | unit |

---

## 十-I、股票预测 - v2 SSE 回归（ping 不落库 · 新事件带 eid）

> 测试文件：`tests/test_prediction_heartbeat.py`（T-PRED-HB-08 起为 v2 新增）。
> v1 已验证的能力，v2 绝不能破坏。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-PRED-HB-08 | 6 个 v2 新事件（TOOL_TRACE / DATA_ITEM / FORMULA_STEP / FORECAST_DAY / EVENT_MARKER / PAUSED）逐个过 `_emit` | 每条都拿到 `eid > 0` 且 `payload["id"] == eid`（前端读的是 `payload.id`）；eid 严格自增（`after_event_id` 游标靠单调性推进）；6 条**全部进 event_log**（可重放）。新事件不带 eid → 用户刷新后卡片/蜡烛要么丢要么重复渲染 | unit |
| T-PRED-HB-08b | 6 个新事件类型 | 均为 `SSEEventType` 枚举成员（铁律 #3，不是字面量） | unit |
| **T-PRED-HB-08c** | FORECAST_DAY → ping → FORECAST_DAY 混合入队 | ★ **v1 的坑不能被 v2 破坏**：新事件走 `_emit` 落库并带 eid；**ping 依旧 `queue.put((0,...))` 直塞、eid=0、不带 id、绝不进 event_log**。ping 若走 `_emit`，一次十几分钟的预测会往 event_log 攒下几百条垃圾行，并在**每次重连 replay 时被全量重放** | unit |

---

## 十-J、LLM 供应商管理（cc-switch：admin 切换全局生效供应商）

> 测试文件：`tests/test_llm_provider_admin.py`。
> 覆盖：`db/llm_provider_store.py`、`services/llm_provider_service.py`、`llm/chat.py` 工厂、
> `routers/admin_router.py` 新端点、`routers/model_router.py`、`db/migrate.py` 幂等。
> 核心不变量：无激活配置时所有 resolve_* 回退 .env（行为与上线前完全一致）；
> 管理端点全部 `X-Admin-Key` 保护（铁律 #1：普通用户 403，非前端隐藏）。

| 用例 | 输入 / 操作 | 期望 | 标记 |
|------|------|------|------|
| T-LLM-P01 | 8 个新 admin 端点（list/create/update/delete/activate/deactivate/test/fetch-models）无 key 或错 key 访问 | 全部 403（verify_admin_access 拦截） | unit |
| T-LLM-P02 | POST 创建（最小字段）/ name 重复 / PUT·DELETE 不存在 id | 创建 200 且 `is_active=false`（新配置默认不激活）；重复 name → 409；不存在 → 404；PUT 只传部分字段时其余字段不被清空 | unit |
| T-LLM-P02b | GET /api/admin/llm-providers | `providers` 含 api_key 明文（仅 admin 可达）；`effective.source` env/db 两态正确，**effective 永不返回 api_key** | unit |
| T-LLM-P03 | activate / deactivate / delete / update 写操作 | activate→`store.activate_provider`+失效广播；deactivate→`deactivate_all`+广播；删激活项/改激活中配置→广播；改非激活配置、新建→不广播（不影响生效值） | unit |
| T-LLM-P04 | `resolve_chat`：None / CHAT_MODEL / SUMMARY_MODEL / ROUTER·SEARCH·MAP 角色名 / 未知名 / 无快照 | 无快照→(LLM_BASE_URL, API_KEY, model or CHAT_MODEL)；快照→快照 base/key + 角色映射（角色模型留空回退 chat_model）；用户显式模型名原样透传 | unit |
| T-LLM-P04b | `resolve_default_request_model` / `visible_models` | 快照 ? 快照.chat_model : CHAT_MODEL；可见模型 = chat_model+extra_models 去重保序 | unit |
| T-LLM-P05 | `resolve_vision`：快照含/不含 vision_model、无快照 | 含→快照三元组（视觉随供应商切换）；不含/无快照→.env VISION_*（回退链 VISION_MODEL > CHAT_MODEL） | unit |
| T-LLM-P06 | `get_chat_llm` / `get_summary_llm` / `get_vision_llm` / `get_vision_async_client` 有/无快照 | 返回实例的底层 base_url 与 model 正确（_http_cache/_client_cache 按 base_url+model 建新条目，旧条目保留不清空）；快照 vision 覆盖时显式 model 让位，env 模式保留旧回退链 | unit |
| T-LLM-P07 | GET /api/models 有/无快照 | 有快照→`[chat_model]+extra_models` 去重，绝不返回 base_url/api_key；无快照→原 Ollama 拉取失败回退 `[CHAT_MODEL]` | unit |
| T-LLM-P08 | `_MIGRATIONS` 全部 DDL 静态检查 + llm_providers DDL 在列 | 所有 CREATE TABLE/INDEX、ADD COLUMN 均 IF NOT EXISTS（二次执行不报错的结构性保证，T-DB-04 无 DB 版）；`llm_providers` 建表 + `ux_llmprov_name` 唯一索引在迁移列表且与 ORM 表名一致 | unit |
| T-LLM-P08b | `_notify_llm_config_changed` 时 DB 刷新与 Redis 广播双双抛异常 | 不向上传播（写操作仍成功，铁律 #9 降级 + 15s 定时兜底） | unit |
| T-LLM-P09 | POST /test 连通性：成功 / 失败（额度不足）/ 传 {id} 测已存配置 / 字段全空 | 成功→ok:true+latency_ms+content 截断；失败→ok:false 且 error 原样透传给前端；{id} 时取库内 base/key/model（显式字段优先）；全空→422 | unit |
| T-LLM-P09b | POST /fetch-models 成功 / 上游超时 | 成功→`{models:[id...]}`；失败→502 detail 带错误信息 | unit |
| **T-LLM-P10** | env 角色名与 CHAT_MODEL **同名**（当前生产 .env 即如此）+ 快照配置了 summary_model / router_model | ★ 名字映射（resolve_chat）同名时退化为 chat_model（已知局限，护栏用例锁定）；**角色感知**的 `get_summary_llm`（resolve_summary）/ `get_router_llm`（resolve_router）仍返回快照角色专属模型；角色留空回退 chat_model；无快照回退 env 角色名 | unit |

---

## 十一、spec 对照检查（改代码后必跑）

### 量化执行隔离与预测恢复回归（2026-10）

| 测试文件 | 关键契约 |
|---------|-----------|
| `test_quant_execution.py` | SDK 超时取消后，真实线程结束前不释放执行额度；不占用 asyncio 默认线程池（DNS/聊天）；异常归还额度；关闭不阻塞事件循环 |
| `test_quant_provider_cancellation.py` | Tushare 批量抓取取消不在事件循环 join；BaoStock 跨实例共用串行 worker，取消不能提前复用 SDK 全局连接 |
| `test_prediction_shutdown.py` | 停机只暂停本 worker 的运行预测并等待落库/租约收尾；终态竞态不误发 paused；DB 卡住时有界退出；延迟量化初始化在停机时取消 |
| `test_prediction_ownership.py` | Redis token 租约只允许一个 worker 执行；旧 owner 不能续期/删除新租约；多订阅者独立游标完整回放；历史暂停不终止续跑；脱离页面后队列有界；暂停/终态不被晚到结果覆盖；DB 行锁下仅显式 resume 可离开 paused |
| `test_prediction_graph.py` T-PRED-GRAPH-07 | 运行完成但中间状态丢失时，按 queued → news_searching → inferring → done 合法补齐；禁止绕 FSM 强写终态 |

| 改了什么 | 跑哪些测试 |
|---------|-----------|
| fsm/ 状态机 | T-FSM-* |
| db/ 数据层 | T-DB-* |
| memory/ 记忆 | T-MEM-* |
| graph/nodes/ 节点 | T-NODE-* |
| graph/runner/ 流式 | T-STREAM-* |
| sandbox/ 沙箱 | T-SANDBOX-* |
| llm/ 客户端 | T-LLM-* |
| graph/edges.py 路由 | T-EDGE-* |
| quant/ 量化模块 | T-QUANT-* |
| quant/ analyze thinking | T-QUANT-ANALYZE-* |
| quant/data/ 财经数据层 | T-FINANCE-DATA-* |
| tools/builtin/finance/ 工具 | T-FINANCE-TOOL-* |
| route_node + finance prompt | T-FINANCE-ROUTE-* |
| prediction/news_search 资讯搜索 | T-PRED-NEWS-* |
| prediction 状态机 (fsm/prediction.py) | T-PRED-FSM-* + T-PRED-CLEAN-* |
| prediction 鉴权 | T-PREDICT-AUTH-* |
| prediction 计算引擎 (engine + indicators) | T-PRED-ENGINE-* (T-PRED-01..16) + **T-PRED-V2-*** |
| prediction 公告三层判定 (graph/prediction/announcements.py) | T-PRED-ANN-* (`tests/test_prediction_announcements.py`) |
| **quant/data/ 公告与 SEC 取数层** | **T-PRED-DATA-*** (`tests/test_prediction_data_layer.py`) |
| **prediction 维度注册表 (dimensions.py / PredictionCriteria)** | **T-PRED-DIM-*** (`tests/test_prediction_dimensions.py`) |
| **prediction v2 持久化 (db/prediction_store.py)** | **T-PRED-STORE-*** (`tests/test_prediction_store_v2.py`) |
| **prediction 暂停 / 续跑 / 删除** | **T-PRED-PAUSE-*** (`tests/test_prediction_pause_resume.py`) + T-PRED-STORE-06/09 |
| **prediction SSE（新事件 / 心跳）** | **T-PRED-HB-*** (`tests/test_prediction_heartbeat.py`) —— 加新 SSE 事件必跑 T-PRED-HB-08* |
| **LLM 供应商管理（admin 切换全局供应商）** | **T-LLM-P-*** (`tests/test_llm_provider_admin.py`) + T-DB-04（迁移幂等）+ T-SMOKE-* |
| prediction 图拓扑 / 断线重连 / 停止 | T-PRED-GRAPH-* / T-PRED-RESUME-* / T-PRED-STOP-* |
| 任何改动 | T-SMOKE-* (冒烟) |
| 新增工具 | T-SANDBOX-03 + T-SMOKE-03 |
| 新增 SSE 事件 | T-FSM-05 + T-STREAM-03（预测链路的还要跑 T-PRED-HB-08*：新事件必须带 eid，ping 必须仍不落库） |
| 新增预测维度 | T-PRED-DIM-01/02（分组不重不漏 + data 维必须有 fetcher）+ T-PRED-DIM-07（data 维不烧 Tavily 预算） |
| 新增 DB 字段 | T-DB-04 (迁移幂等) |
| 新增 quant provider | T-QUANT-05 + T-QUANT-06 |
| 文件上传相关 | T-DB-02 + T-SANDBOX-04 + T-UPLOAD-* |

### 预测轻量恢复与完整原文（2026-10）

- `GET /api/prediction/{id}?compact=true` 在同一个只读 REPEATABLE READ 事务中读取快照与事件投影，返回 `event_cursor` 和按事件 ID 聚合的 `stream_text`。游标前的 token、结构化产物、DONE 结果必须已体现在恢复视图中；禁止另查 latest ID 后直接跳过日志。
- 新闻列表仅下发来源标题、URL、500 字符摘要预览和 `source_index`；原始 `full_content` 与完整摘要仍原样存库。`GET /{id}/source?dimension=...&source_index=...&url=...` 读取单个完整来源，包含事件已提交但快照未写入的回退路径。无 compact 参数的旧接口保留全量契约。
- `GET /{id}/stream?compact=true&after_event_id=...` 在 SQL 端裁剪 NEWS_ITEM 和 DONE 内嵌新闻正文，保留增量事件 ID；页面先展示恢复视图，再只消费游标后的事件，增量重连不得清除既有思考/结果。
- `tests/test_prediction_view.py`：覆盖全部结构化事件投影、原始文本完整性、1001 条 token 顺序、legacy type/sse_string、快照读取和事件读取之间并发提交、历史 marker score 保留及暂停 CAS 失败。
- 前端 `workspaceLifecycle.test.mjs`：覆盖 checkpoint 续传、0 游标快照保留、buffered token 重连不丢失、创建请求晚到、列表请求去重/旧市场结果拒收及卸载后不恢复活跃详情。
