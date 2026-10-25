# ChatFlow 系统可用性审计与修复记录（2026-10-25）

本轮继续排查本地部署的卡顿、恢复缓慢、历史内容与文件产物异常，覆盖后端执行、持久化、身份校验及前端生命周期。重点是把已发现的问题修复到真实链路，并验证历史数据仍可读取。**这不是“所有代码已无 Bug”的保证，也不是对全部模型、行情源和并发规模的认证。**

本文区分上一轮已落地的基础修复与本轮新增工作。前一轮的执行器、租约、停止与优雅退出等机制见 [可用性修复与 Codex 对照](reliability-review-2026-10-25.md)。本轮代码已完成回归，并部署到本地生产环境，验收结果见末节。

## 1. 覆盖范围与审计方法

| 领域 | 本轮检查的主要链路 |
|---|---|
| 预测恢复 | 快照查询、新闻正文、事件日志、思考文本、SSE 游标、暂停/继续、历史图表、列表轮询 |
| 对话恢复 | 页面快照与模型内存、启动加载、失效通知、消息身份、历史工具调用、事件分页与追尾 |
| 文件产物 | 工具落库、消息归属、同路径跨轮覆盖、实时事件、历史列表、下载与预览 |
| 交互与生命周期 | 切换会话、卸载、重连、重复发送、重新生成、编辑重发、上传、滚动跟随、删除 |
| 身份与权限 | Access/Refresh Token、并发刷新、匿名 client_id、退出登录竞态、OAuth state、用户设置 |
| 上下文与推理 | 工具协议配对、语义缓存、长期记忆超时、摘要压缩、量化/预测上下文引用 |
| 执行基础 | 复核前一轮线程池隔离、预测租约、流式持久化、取消与退出机制的相关回归 |

采用引用检索、关键实现审查、隔离依赖测试、真实 PostgreSQL/Redis 测试，以及现有最大预测记录的只读对比。测试使用专门的回归运行器和独立测试库；生产样本对比不启动新预测、不改写原始记录。

## 2. 已确认的问题与本轮修复

### 2.1 预测恢复与事件传输

原先打开详情会先下载包含完整新闻正文的快照，再以 `after_event_id=0` 重放事件；前端开始重放时又清除刚恢复的结果。完成事件还内嵌一整份新闻正文，单独裁剪 `news_item` 无法解决问题。

本轮增加以下机制：

- `GET /api/prediction/{id}?compact=true` 返回轻量恢复视图、`event_cursor` 与按事件 ID 聚合的 `stream_text`。旧的无 compact 接口保留全量契约。
- 快照列与事件投影在同一个只读 `REPEATABLE READ` 事务内读取。游标之前的内容必须已投影到视图，不能仅查最新事件 ID 后跳过历史。
- 处理“节点已发出事件、快照尚未写入”的窗口；读取快照后才提交的事件留在游标之后，由增量流补齐。
- 在 PostgreSQL 内裁剪 `NEWS_ITEM` 和 `DONE.news_digest` 的正文，再交给 Python/浏览器解码。正文与完整摘要仍原样存库；预览摘要限 500 字符，用户展开来源时通过单条来源接口读取完整文本。
- 前端先展示恢复视图，再按其游标续传；增量重连保留旧思考和未刷新的缓冲文本，零事件游标的已有快照也不会被清空。
- 列表、活跃任务查询和更新准备查询只读需要的列，避免只为状态或进度反复解码大段新闻 JSON。
- 保留 DB 状态机为状态真相源；历史 `paused/done` 不覆盖当前状态。暂停/停止状态转换未成功时不再报告成功。
- 修复预测页面异步初始化期间卸载后仍创建定时器、重复列表请求、旧市场响应覆盖新列表及迟到的创建响应劫持导航。

实现入口：[prediction_view.py](../llm-chat/backend/db/prediction_view.py)、[prediction_store.py](../llm-chat/backend/db/prediction_store.py)、[prediction_router.py](../llm-chat/backend/routers/prediction_router.py)、[usePrediction.ts](../llm-chat/frontend/src/composables/usePrediction.ts)、[PredictionNewsSource.vue](../llm-chat/frontend/src/components/PredictionNewsSource.vue)。

### 2.2 对话恢复、历史与删除

| 确认问题 | 修复结果 |
|---|---|
| 页面读取对话会重建模型使用的内存缓存，与正在运行的新轮竞争 | 分离只读 UI 投影与执行历史加载；实际开始执行时再加载模型历史 |
| 启动或缓存失效会批量读取完整历史，恢复链路重复查询工具记录 | 按需加载，独立 UI 查询并发执行，工具记录一次读取 |
| 每条历史 SSE 单独触发 ASGI 写出和断连检测 | 按约 32 KiB 合并完整 SSE 帧；每帧保留自身事件 ID，超大单帧不被破坏 |
| 停止确认或认领前孤儿检查可能重建模型缓存 | 停止确认使用只读投影，认领前 reconcile 不加载执行历史，避免覆盖另一标签页刚开始的新轮 |
| 恢复消息缺少明确的用户/助手轮次身份，旧响应可能覆盖新轮 | 补全轮次身份与前端版本检查；迟到响应不能替换当前流 |
| 历史工具调用仅凭“下一条是 ToolMessage”判定完整 | 按 tool_call_id 配对，过滤未配对/重复回复，避免恢复后的工具协议错误 |
| 删除时取消图后立即删数据，消费者还可能继续写入 | 等待执行与持久化收尾；超时保留任务和停止信号；数据库事务内锁定并校验状态后清理 |
| 批量删除部分失败却在页面清空全部选中项 | 返回实际 `deleted_ids` 与失败项，前端只移除确认删除的记录 |
| 标题/提示词修改通过整个缓存对象保存，可能覆盖活跃历史 | 只更新指定字段 |

相关实现：[memory/store.py](../llm-chat/backend/memory/store.py)、[conversation_service.py](../llm-chat/backend/services/conversation_service.py)、[graph/runner/stream.py](../llm-chat/backend/graph/runner/stream.py)、[BaseNode](../llm-chat/backend/graph/nodes/base.py)、[useChat.ts](../llm-chat/frontend/src/composables/useChat.ts)。

### 2.3 文件产物、上传与预览

旧的产物 upsert 按“对话 + 路径”匹配，新轮写同一路径会覆盖原始内容并迁移消息归属。页面又可能把所有产物放到最新消息，造成“历史文件消失或变成新文件”。

本轮将持久化身份细化为消息、来源与路径，并通过事务级锁串行化同一产物的并发写入。不同消息保留独立版本；同一消息内同一路径仍是更新语义，不代表每次编辑都有版本历史。工具事件贯穿持久化 artifact ID 和消息 ID，元数据事件不能用空正文覆盖已保存内容，保存失败不再静默报告成功。

前端按消息归并产物，保留跨轮历史；无消息 ID 的旧产物只放入总览，不猜测所属轮次。预览采用可取消请求与版本检查，旧请求不能覆盖新选中的文件。下载使用带身份的请求；上传等待真实会话 ID，切换会话、删除附件或卸载后使旧上传结果失效。对象 URL、读取任务和预览状态按生命周期清理。创建首个会话时使用覆盖层保留 InputBox 实例，防止创建过程卸载输入框而取消上传、丢失草稿；真实 Vue 模板集成测试覆盖首次创建时的两附件与草稿保留。

**历史限制：此前已经被覆盖的产物内容，在当前数据库中没有旧版本可读，无法由本轮代码自动重建。** 若另有备份、沙箱副本或原始文件，可另行核对恢复；本轮没有声称恢复了这些已丢失字节，也没有伪造旧版本。

相关实现：[artifact_store.py](../llm-chat/backend/db/artifact_store.py)、[artifact_handler.py](../llm-chat/backend/graph/runner/handlers/artifact_handler.py)、[messageArtifacts.ts](../llm-chat/frontend/src/composables/messageArtifacts.ts)、[useArtifactPreview.ts](../llm-chat/frontend/src/composables/useArtifactPreview.ts)、[InputBox.vue](../llm-chat/frontend/src/components/InputBox.vue)。

### 2.4 身份、上下文与前端交互

| 领域 | 本轮行为变化 |
|---|---|
| 登录失效 | 已提供但无效的 Bearer Token 返回 401，不再悄悄降级成匿名身份而“丢失历史” |
| HTTP 请求 | JSON、multipart、流式和下载复用身份传输；多个 401 共用一次刷新，取消和登录代次变化会阻止旧请求重试覆盖新身份 |
| 登录生命周期 | 初始化去重；晚到的登录/初始化/退出响应不能改写新的身份状态 |
| 后端身份校验 | OAuth state 原子单次消费；刷新时确认用户有效；管理员密钥空值拒绝，比较使用恒定时间函数 |
| 资料与设置 | 更新接口实际持久化并校验字段，不再只返回成功 |
| 语义缓存 | 带文件、图片、显式上下文及多轮依赖的请求避免错误命中；缓存故障或超时作为未命中继续推理 |
| 可选检索与摘要 | 记忆/缓存检索和写入有界等待；摘要设定超时，空摘要不推进压缩游标、不替换原始上下文 |
| 快照接入对话 | 量化/预测引用先校验归属；预测只读取结论、价格、风险和参数等字段，不把十余 MB 新闻正文重复塞入上下文 |
| 用户阅读位置 | 聊天滚动按动画帧合并，观察实际内容高度；主动查看历史时不强行跳到底部，卸载清理观察器和定时任务 |
| 多轮交互 | 重复发送、重新生成、编辑重发、删除失败及迟到请求采用身份/版本校验，保留已生成内容和历史产物 |

默认新增等待配置为 `CONTEXT_LOOKUP_TIMEOUT=5` 秒、`SUMMARY_TIMEOUT=15` 秒，可按实际服务配置调整。这些上限控制可选依赖等待，不保证模型或行情服务在固定时长内完成。

## 3. 预测前后实测

使用本地生产数据库中按新闻 JSON 文本体积选出的最大预测记录，只读比较旧快照与新恢复视图。数值为单次样本测量，不是并发压测或浏览器首屏时间基准。

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| 序列化快照/恢复视图（UTF-8，未压缩） | 10,520,613 bytes | 489,400 bytes |
| 传输体积减少 | — | 约 95.35% |
| 打开运行中详情后的历史重放 | 另从 0 重放约 21 MB 日志 | 从恢复视图游标后读取增量 |
| 新恢复视图数据库读取与投影 | 初版实现约 2.3 秒 | 优化后约 166.9 ms |

旧全量快照单次数据库读取约 72.8 ms。不能将新投影的 166.9 ms 表述为“数据库查询全面更快”：收益主要是减少接口、网络、浏览器解码及重复事件处理；新投影包含旧接口没有提供的事件一致性处理。中间版本的 2.3 秒主要来自 PostgreSQL JIT 和重复 JSON 变换，已通过局部关闭 JIT、物化中间结果和避免重复复制现代事件 payload 降低。

同一记录直接比较以下 16 个命名字段，语义一致：状态、analysis、risk_notes、model_params、historical_bars、predicted_price、predicted_range、confidence、news_sentiment、event_score、news_digest、filing_digest、formula_steps、forecast_candles、event_markers、tool_traces。对比排除传输元数据、正文按需引用和 `stage=key` 兼容别名；新闻全文另由测试验证逐字保留。严格的逐字段比较清单与最终线上响应测量由部署验收补充。


## 4. 与 Codex CLI 的机制对照

对照对象为本机克隆的 OpenAI Codex 源码提交 `970111f6cb62cc02fe36a82ea7d71b37a2889c97`，不是对 Codex 当前所有产品形态的推断，也没有进行相同模型、相同任务的速度或准确率 A/B 测试。

| 机制 | 已核实的 Codex 源码入口 | ChatFlow 当前边界 |
|---|---|---|
| 线程历史与恢复 | [thread_manager.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/thread_manager.rs)、[rollout/recorder.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/rollout/src/recorder.rs) | ChatFlow 已围绕 DB 状态、消息身份、持久化事件和游标恢复补齐可靠性；预测仍是保留已完成维度后重跑未完成部分，不是任意节点原地恢复 |
| 子 agent 控制 | [agent/control](https://github.com/openai/codex/tree/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/control) 包含 spawn、resume、mailbox 等控制实现 | ChatFlow 的并发工具调用不等于独立 subagent；本轮未交付产品级父子 agent 关系、信箱、独立预算和生命周期恢复系统 |
| 交互进程 | [unified_exec/mod.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/unified_exec/mod.rs) 具有 process_id、stdin/yield 及进程数量约束 | ChatFlow 当前仍基于 SSH 沙箱任务；前一轮修复取消与输出边界，不等价于可恢复 PTY 与持久化交互进程会话 |
| 采样与推理预算 | 本轮没有验证 Codex 各模型/角色的全部采样策略 | ChatFlow 前一轮增加按角色采样与输出预算配置；本轮补齐上下文与工具协议可靠性。不能据此声称采样策略或推理质量与 Codex 对齐 |

本次开发使用多个协作代理，只说明审计工作的组织方式，不代表 ChatFlow 产品已获得同等多 agent 能力。

## 5. 测试证据与复现方式

最终回归结果：**后端 543 通过、3 跳过；真实数据库集成另行 8 通过；前端 61 通过。** 真实服务测试在独立配置下执行，skipped 不计作通过。

```sh
cd llm-chat/backend
python tests/run_regression.py -q tests
```

真实数据库/Redis 测试需显式提供隔离测试服务配置，库名必须以 `_test` 结尾；不应把生产数据库配置作为测试目标。

```sh
cd llm-chat/frontend
npm test
npm run build
```

| 回归文件/类别 | 核心证据 |
|---|---|
| `test_prediction_view.py` | 1001 条 token 顺序、快照/事件并发提交窗口、事件先于快照、legacy type/SSE 字符串、NEWS_ITEM/DONE 正文裁剪、完整原文回读、marker score 保留、暂停 CAS 失败 |
| `test_chat_recovery_performance.py` | UI 读取不替换模型缓存、独立读取并发、SSE 成帧批量输出、半页断连续传、大事件保持完整、按需历史加载 |
| `test_artifact_ownership.py` | 跨消息同路径版本独立、并发重复写入、持久化身份贯穿流式事件 |
| `test_context_reliability.py` / `test_tool_history_protocol.py` | 可选依赖超时、缓存跳过条件、引用权限、工具调用与响应配对 |
| `test_conversation_delete.py` | 等待持久化消费者、超时不强删、事务内锁与状态校验、批删精确结果 |
| 前端 HTTP/身份/上传/预览/滚动/工作台测试 | 刷新去重、身份竞态、取消、消息与产物归属、旧响应失效、上传生命周期、卸载不创建轮询 |

测试文件与 [backend/test_case.md](../llm-chat/backend/test_case.md) 是可复现契约；测试数量不等于全仓覆盖率，也不替代真实浏览器、外部服务和长期运行验证。

## 6. 已知边界与后续工作

- 已被旧逻辑覆盖的历史产物内容无法从当前记录自动重建；修复保障新写入的跨消息归属与内容独立。
- 同步行情 SDK 的底层线程无法强制终止。前一轮隔离与限流避免阻塞事件循环或提前归还容量；永久卡死仍需要进程级隔离才能硬终止。
- 预测暂停/恢复会重跑未完成维度；尚无覆盖整条图的逐节点持久化 checkpointer。
- 智能选股的旧后台任务系统未统一成预测租约体系；默认关闭功能不据此宣称具备完整跨进程恢复能力。
- 完整 subagent、可恢复 PTY、统一精确 token 预算仍是独立建设项；本轮未实现。长对话尚未引入消息虚拟列表。
- 外部模型、检索和行情服务的限流、时延与结果质量无法由本轮代码修复保证；没有验证预测收益或准确率。
- 上述生产数据仅为只读功能/体积样本，未完成高并发压力、长时间运行、所有浏览器及所有外部供应商组合的穷尽测试。

## 7. 本轮部署与最终验收

2026-10-25 已重建并更新本地生产 compose 的 backend/frontend。发布镜像沿用 `chatflow-backend:1.0.9-prod`、`chatflow-frontend:1.0.9-prod` 标签；两个生产容器健康，4 个 API worker 启动完成，本轮启动及冒烟日志无 traceback/ERROR。发布前没有处于执行状态的会话或预测。

最终验证：

- 后端全量回归 **543 passed、3 skipped**；跳过的真实服务测试单独配置隔离 PostgreSQL/Redis 运行，**8 passed**。另有 1 条上游依赖弃用警告。
- 正式 Docker 前端构建执行 **61 项测试全部通过**，随后 `vue-tsc` 与 Vite 构建通过。仍有较大 JS chunk 的体积提示，未把构建成功等同于首屏性能达标。
- HTML 入口采用 DOMPurify 清理，4 组真实 DOM 测试覆盖危险链接、事件属性、SVG/MathML 与正常公式、表格、代码、图片。同步修复 marked v13 renderer 参数不兼容及 data URI 占位符被 Markdown 解析的问题。测试依赖 jsdom 固定为支持发布容器 Node 20 的 26.1.0。
- 真实 HTTP/模型：原始 SSE 断开后，两个独立订阅者收到相同的 8 个持久化事件；完整恢复约 3.04 秒。错误匿名身份访问返回 403；推理中停止约 2.98 秒确认，DB 收尾完成。10 路轻接口并发最大约 42.9 ms。
- 预测创建→暂停→读取 paused→恢复→停止→读取 stopped 通过；预测执行期间 10 路轻接口并发最大约 9.3 ms。
- 真实 Chromium 页面：在临时会话构造第一轮持久化文件，发送第二轮真实模型对话，确认文件仍归属第一轮；刷新后归属和内容保持。带身份下载逐字核对正确。首次无会话上传成功，草稿保留、文件落库且预览可读取；两项页面测试均无浏览器运行时异常。
- 最大已有预测记录的部署后 HTTP 测量：全量响应 **10,518,646 bytes / 493.5 ms**，compact 响应 **476,013 bytes / 297.2 ms**，未压缩响应体积减少 **95.47%**，游标为 191494。完整新闻来源按需读取成功。这是单次本地功能与体积测量，不是高并发或公网延迟基准。
- 所有验收临时会话与预测均已通过 API 删除；已有用户记录未被测试改写。测试专用容器与网络在验收后清理。

部署前备份：`~/workspace/backups/chatflow/pre-system-audit-20261025.dump`（约 290 MB，权限 600，`pg_restore --list` 成功读取目录）。回滚镜像为 `chatflow-backend:pre-system-audit-20261025` 与 `chatflow-frontend:pre-system-audit-20261025`。没有将备份目录可读表述为已经执行完整恢复演练。

代码提交包含本报告；最终提交哈希与推送状态以交付消息及 Git 历史为准。
