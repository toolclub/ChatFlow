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

## 十、spec 对照检查（改代码后必跑）

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
| 任何改动 | T-SMOKE-* (冒烟) |
| 新增工具 | T-SANDBOX-03 + T-SMOKE-03 |
| 新增 SSE 事件 | T-FSM-05 + T-STREAM-03 |
| 新增 DB 字段 | T-DB-04 (迁移幂等) |
| 文件上传相关 | T-DB-02 + T-SANDBOX-04 + T-UPLOAD-* |
