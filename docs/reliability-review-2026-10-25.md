# ChatFlow 可用性修复与 Codex 对照（2026-10-25）

本次围绕本地部署中「预测导致卡顿」「对话无法恢复」进行代码排查、修复和回归。保留 PostgreSQL、Redis、LangGraph 与 Vue 的现有架构，新增幂等索引迁移，不删除历史数据。

## 已确认的原因与修复

| 链路 | 原问题 | 本次实现 |
|---|---|---|
| 行情与统计计算 | 行情 SDK、缓存和 ARIMA/GARCH 占用 asyncio 默认线程池，连 DNS 都会排队；取消协程后底层线程仍工作却提前归还并发额度 | 独立有界 IO/计算/磁盘执行器，底层任务真正结束后释放额度；BaoStock 独立单线程保证全局 socket 串行 |
| Tushare | `ThreadPoolExecutor` 上下文退出同步等待线程，取消/超时反而阻塞事件循环 | 共享有界执行器，取消路径不在事件循环 join 线程 |
| 预测任务所有权 | 多 worker 中「本进程没有任务」被当成「任务已死」，重连可能重复运行；多个标签页抢一个队列 | Redis token 租约；比较 token 续期/释放；每个订阅者独立从 event_log 回放；所有权失效停止旧任务 |
| 预测暂停与恢复 | 旧暂停事件截断新流；晚到完成覆盖 paused/stopped；新 worker 启动清理其他 worker 的活跃任务 | 行锁下通过 FSM 校验状态；恢复先抢租约；历史暂停按当前 DB 状态处理；仅清理失去 owner 且超过宽限期的任务，改为可继续的 paused |
| 对话停止与鉴权 | stop 路由实际没有实现；匿名恢复所需 client_id 未从存储层返回 | 真正取消模型任务，等待持久化收尾后握手确认；补全匿名归属校验 |
| 对话回放 | 实时 SSE 没有持久化游标，DB→内存尾部切换漏事件，跨轮可能重放旧终态 | 事件提交后才发出，携带 id/message_id；从同一 DB 日志分页回放与追尾；请求中断后只 GET 重连，不重复 POST |
| 对话并发与进程中断 | 同一会话两个请求可能互相清理；崩溃后一直 streaming | 会话行锁原子认领；基于 Redis 存活状态与 DB 时间戳恢复孤儿，保留部分输出与继续能力 |
| 服务重启 | 后台初始化任务无句柄，依赖关闭与仍在运行的图竞争 | 有界取消初始化；预测暂停并保留已完成结果，对话保存部分响应后再关闭依赖 |
| 并发工具 | 单个 current_tool_seq 把同名并发工具的输出/完成写错行 | LangGraph run_id 贯穿事件、DB 关联与前端匹配；未完成工具在取消/失败时终态化 |
| 推理客户端 | 同 endpoint/model 换 key 复用旧客户端；usage 与正文同 chunk 时丢正文；异常返回部分结果后误判成功 | 缓存键包含凭据与角色配置；同时处理 usage/正文/推理；请求总时限、流 close、长度截断报错；异常交给 runner 保存部分结果 |
| 状态判断 | 反思节点通过工具输出包含 error/failed 推断失败 | 当前批次 ToolMessage 结构化状态 + DB 执行状态，禁止文本推断；DB 迟到不掩盖本批失败 |
| 终端输出 | 取消后辅助任务悬挂，输出队列及累计文本可持续增长 | 回收读取/等待/监控任务；有界队列；stdout/stderr 累计 1 MiB，上限后提示并继续排空进程输出 |
| 数据库连接 | 原每进程 20+40，4 worker 可占 240 连接，超过 PostgreSQL 常见默认容量 | 默认每 worker 5+5，等待上限 10 秒；增加 `(conv_id,message_id,id)` 回放索引 |
| 前端恢复与性能 | 断流吞错、一直 loading；旧回调覆盖新流；加载图表最多等待 15 秒才连 SSE；历史只恢复最后一份计划 | 有界退避重连、显式连接反馈、连接身份与视图版本校验；先快照/SSE 后异步图表；恢复所有历史计划；轮询不重叠且卸载失效 |
| 发布与首屏 | 正式类型检查失败，Docker 构建绕过检查；量化图表等全部进首屏 | 修复类型错误，Docker 执行测试及正式构建；量化/预测/管理/流程图按需加载。此环境首屏入口 JS 从约 2.98 MB 降至 1.82 MB（未压缩构建产物，非交互耗时基准） |

## 采样与推理预算

新增可选环境配置 `LLM_ROLE_OPTIONS`，角色为 `chat/router/summary/vision`。不配置时保持既有采样；DeepSeek 的 router 角色关闭原先强制的高推理配置，其他角色保留兼容行为。厂商扩展可通过角色的 `extra_body` 覆盖。

```dotenv
LLM_ROLE_OPTIONS={"router":{"temperature":0,"max_tokens":512},"summary":{"max_tokens":2048},"chat":{"max_completion_tokens":8192}}
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=5
DB_POOL_TIMEOUT=10
QUANT_IO_WORKERS=8
QUANT_COMPUTE_WORKERS=2
```

`max_tokens` 和 `max_completion_tokens` 按所部署厂商支持的协议二选一，不可同时配置。以上预算仅是配置示例，没有擅自写入本地部署的 API 凭据或模型设置。线程数按 API worker 数相乘计算；已有生产 compose 限制了 BLAS/OpenMP 内部线程。

## 与 Codex 的差距

源码对照：本机 `~/workspace/codex`，OpenAI 官方仓库提交 `970111f6cb62cc02fe36a82ea7d71b37a2889c97`。对照的是该提交的实现，不能把当前 ChatFlow 的 ToolNode 并发称为完整 subagent 能力。

| 能力 | Codex 对照位置 | ChatFlow 当前结果与边界 |
|---|---|---|
| 会话持久化/恢复 | [thread_manager.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/thread_manager.rs)、[rollout recorder](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/rollout/src/recorder.rs) | 已补 durable 事件回放、跨 worker 续流和部分结果保存；并非恢复任意工具指令执行到某一 CPU 指令 |
| 子 agent 生命周期 | [agent/control](https://github.com/openai/codex/tree/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/control)：spawn/resume/mailbox/interrupt/execution | 目前没有独立 agent 表、父子关系、信箱、配额与状态恢复；本轮做的是并发工具可靠性。引入产品级 subagent 仍需独立设计及端到端验收 |
| CLI/交互进程会话 | [unified_exec](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/unified_exec/mod.rs)：process_id、stdin、yield、输出与进程上限 | 当前仍为 SSH 沙箱执行。本轮修复取消、超时和输出上限；未实现可恢复 PTY session、stdin 与持久化进程注册表 |
| 上下文与预算 | Codex 的线程历史、恢复与执行配置相互配合 | 当前仍以消息窗口/摘要/截断为主；新增分角色采样与输出预算，不等同于精确 token 计量和自动上下文压缩 |

官方说明参考：[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。上述实现判断以实际源码为依据。

## 验证方式

后端需要项目依赖及 `pytest pytest-asyncio`：

```sh
cd llm-chat/backend
python tests/run_regression.py -q tests
```

`run_regression.py` 使用测试配置与无效外部端点，覆盖可选凭据，避免测试意外使用部署配置。真实数据库测试默认跳过；必须显式设置 `CHATFLOW_TEST_DATABASE_URL`（库名以 `_test` 结尾）与 `CHATFLOW_TEST_REDIS_URL`，再执行 `tests/test_db_recovery_integration.py`。该测试已在独立 PostgreSQL/Redis 容器验证迁移幂等、8 路并发认领、1001 条正文跨页回放、匿名归属、孤儿时间戳保护、真实租约及预测 FSM。

```sh
cd llm-chat/frontend
npm test
npm run build
```

本次最终后端全量回归：503 passed、1 skipped（真实数据库测试单独运行通过），另有 1 条依赖弃用警告。前端 14 项测试通过。

前端回归包括游标去重、网络首连/续传失败、终止取消、旧连接回调、跨预测切换、非阻塞图表加载、卸载后不再轮询、网络失败保留部分正文及停止握手期间禁止重复发送。`backend/test_case.md` 记录后端各链路的验收契约。

## 保留边界

- 同步行情 SDK 的线程无法强制杀死。本轮保证取消不阻塞事件循环、不提前释放底层容量，也不占用默认 DNS 池；永久卡死的第三方 SDK 仍需进程隔离才能硬终止。
- 股票预测恢复保留已完成维度，未完成维度会重跑；当前图没有逐节点持久化 checkpointer。
- 外部行情接口的限流/可用性与模型供应商的实际耗时不能由本次修复保证。
- 智能选股（非股票预测）的旧后台任务所有权体系尚未统一成预测租约体系，默认关闭的筛选功能不应据此宣称已具备全部跨进程恢复能力。


## 本地部署验证

2026-10-25 已重建并更新本地生产 compose 的 backend/frontend，两个容器健康，4 个 API worker 正常启动，回放索引已存在。更新前保存数据库自定义格式备份（`~/workspace/backups/chatflow/pre-reliability-20261025.dump`，已验证目录可读）与 `chatflow-backend:pre-reliability-20261025`、`chatflow-frontend:pre-reliability-20261025` 旧镜像。

真实 HTTP/模型冒烟结果：

- 原始会话连接主动断开后，两个独立 SSE 订阅者收到相同的完整持久化事件；刷新可读已完成正文，约 4.17 秒完成。
- 错误匿名 client_id 访问返回 403；推理中停止约 0.16 秒确认，DB 已完成收尾。
- 预测创建→暂停→读取 paused→恢复→停止→读取 stopped 通过；运行期间并发 10 路轻接口请求最大约 52.4 毫秒。
- 临时会话和预测记录验证后删除。上述为本地单次功能冒烟，不是生产压力测试，也不代表模型预测准确率或完整行情计算耗时。

此环境没有可用浏览器自动化入口，因此没有声称完成视觉验收；前端以正式类型构建、传输与真实 composable 生命周期测试验证。
