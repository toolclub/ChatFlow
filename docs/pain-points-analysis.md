# ChatFlow 智能体痛点分析

_日期：2026-04-25（更新）_
_范围：`llm-chat/backend/` 的 prompt / memory / graph 三层_
_结论一句话：不是提示词写得差，而是提示词体系还没产品化。_

---

## 0. TL;DR

ChatFlow 目前是一个**"带摘要 + 向量检索的对话 Agent"**，先进的开源 Agent 已经是**"有上下文操作系统的 Agent"**。

差距不在某一条 prompt 的文案，而在三件事上：

1. **上下文分层** —— 平台规则 / 项目规则 / 节点规则 / 工具规则全塞在一个 system prompt 里。
2. **按需加载** —— 工具 guidance 一次性全量注入，没有 trigger / progressive disclosure。
3. **可演化记忆** —— 长期记忆只是"压缩时把 Q&A 丢进 Qdrant"，没有事实抽取、没有更新/删除、不跨会话、不跨用户。

---

## 1. 当前设计的优点（不能推翻的基础）

先说清楚哪些已经做对了，改造时不要顺手砸掉：

| 方向 | 现状 | 位置 |
| --- | --- | --- |
| Prompt 独立目录 | 不再塞在代码里 | `prompts/` |
| 节点 Prompt 拆分 | route / planner / reflector / call_model_step 分开 | `prompts/nodes/*.md` |
| Skill 就近放置 | 工具自带 GUIDANCE / ERROR_HINT，自动发现 | `tools/skill.py:90`, `tools/skill.py:207` |
| Prompt 落盘 | 每次请求的 prompt 都有日志 | `logging_config.py` |
| 三层记忆意识 | 滑窗 + 中期摘要 + 长期 RAG | `memory/context_builder.py:54`, `memory/compressor.py:40` |
| 忘记模式 | 不是二元，而是相似度阈值 | `rag/retriever.py:141` |

已经做到 1.0。下面的问题都是"离 2.0 还差的结构差距"。

---

## 2. 提示词体系的痛点

### 2.1 行为规则散在三层，冲突时没人裁决

当前规则分散在：

- 全局 system prompt：`prompts/system.md:1`
- 节点 prompt：`prompts/nodes/planner.md:1`、`prompts/nodes/call_model_step.md:1`
- 工具 GUIDANCE：每个工具自带，由 `SkillRegistry.build_guidance()` 汇总，见 `tools/skill.py:207`
- 代码里还有**硬编码 prompt 片段**：`graph/nodes/call_model_node.py:176` 的 `_build_step0_messages`、`:244` 的 `_build_focused_step_messages` 都会拼装 system content。

冲突示例：

- `system.md:13` 要求"文件交付优先，不要重复粘贴大段产物"。
- `planner.md:11` 又说"最后一步必须直接输出该产品的完整内容（代码/HTML/文档）"。
- 这两条在"生成网页"场景下天然打架，只能靠模型自己权衡。

**问题**：没有优先级表，没有"谁覆盖谁"的规则。

### 2.2 工具 Guidance 全量拼入 system prompt

`memory/context_builder.py:45-52`：

```python
from tools import get_tools_guidance
guidance = get_tools_guidance()
if guidance:
    system_parts.append(f"\n{guidance}")
```

早期没问题，工具一多就会变成"规则垃圾场"。

对比成熟方案：

- **OpenHands** 把 skills 分成「常驻 AGENTS.md」「关键字触发的 skill」「agent 主动读取的完整 skill 文档」三档（progressive disclosure）。
- **Letta** 的 memory blocks 才是常驻项，工具文档不是。

**现在就能观察到的副作用**：

- 无论 route 是 `chat` / `code` / `search` / `search_code`，所有工具 guidance 都进 system。`chat` 路由甚至不需要工具，但 guidance 还在消耗 context。
- 新加工具 = 所有请求的 system prompt 都变长，token 成本线性上升。

### 2.3 Prompt 是"文案驱动"，不是"协议驱动"

典型例子：澄清协议（`prompts/system.md:18-24`）要求模型输出

```
[NEED_CLARIFICATION]{"question": ..., "items": [...]}[/NEED_CLARIFICATION]
```

这是字符串包 JSON，靠正则从文本里抠出来。能跑，但脆：

- 模型稍微多输出两行解释就会污染边界。
- 想换协议（比如改成 tool call）成本高，前端解析器也要跟着改。

更成熟的做法：

- 结构化输出（JSON Schema / response_format）
- 独立 tool：`request_clarification(...)` 作为一等工具
- function_calling 返回结构化字段

现状：澄清信息被两层兜住——
① prompt 层让模型输出字符串包 JSON
② 代码层 `graph/nodes/call_model_node.py:34` 的正则 `_WEBPAGE_KEYWORDS` 做**硬编码兜底**

硬编码兜底说明模型层的协议不够稳。

### 2.4 没有层级优先级模型

你现在缺一份明确的"上下文优先级表"：

```
Platform（ChatFlow 身份、反查规则）
  > Project（本仓库的工作规范、文件交付约定）
    > Node（planner / reflector / call_model_step 的职责约束）
      > Tool（当前绑定工具的使用指导）
        > User session（本次用户偏好、澄清结果）
```

Letta 把这层关系写死在架构里——core memory blocks、archival memory、external files 各有明确地位。
ChatFlow 现在全塞在一段 `system_parts` 里（`memory/context_builder.py:41`），谁先谁后靠拼接顺序。

### 2.5 可观测但不可评估

✅ `logging_config.log_prompt` 会把每次请求的 prompt 落盘。
❌ 但没有：

- Prompt version
- 回归样本集（改 planner.md 之后怎么验证没坏老 case？）
- 「某条 prompt 改动影响哪些行为」的映射表

换句话说：**能看到 prompt，不能运营 prompt**。

### 2.6 没有 repo/project 级别的一等上下文

OpenHands 把 repo 级规范（AGENTS.md、setup 脚本、pre-commit）当常驻上下文。
ChatFlow 只有"平台通用 prompt + 工具 guidance"，用户/项目/团队自己的长期规范没有独立层。

**例子**：用户说"我们团队的 PPT 都要用 16:9、浅色主题"，这种偏好在当前架构里无处安放：

- 放 system_prompt？会被所有会话继承（不想要）
- 放 mid_term_summary？会被下一次压缩冲掉
- 放 Qdrant？只有相关 query 才会召回

---

## 3. 记忆体系的痛点

### 3.1 长期记忆是 "conversation retrieval"，不是 "agent memory"

`rag/retriever.py:122-129`：

```python
query_filter=Filter(
    must=[FieldCondition(key="conv_id", match=MatchValue(value=conv_id))]
),
```

**所有检索都按 conv_id 过滤。**
也就是说记忆只服务于"当前这一个会话"，不跨：

- 用户（同一个人的不同对话学不到东西）
- 项目（同一个仓库的不同任务学不到东西）
- 团队（共享偏好无处挂）

`memory/schema.py:22` 的 `Conversation` 连 `user_id` 字段都没有，只有 `client_id`（浏览器本地生成的随机串）。这意味着换个浏览器连身份都认不出来，更别说跨会话的 agent memory。

### 3.2 写入时机太晚

`rag/ingestor.py:18` 明确注释：
> 仅在压缩触发时调用，不在每轮对话后写入（批量更高效）。

压缩触发条件 = `unsummarised >= COMPRESS_TRIGGER * 2`（见 `memory/context_builder.py:171`）。
默认配置下大概要攒十几轮才触发一次。

**实际后果**：

- 新偏好不能立刻成为长期记忆。
- 短对话里的重要信息永远不会被记住——因为没到压缩阈值对话就结束了。
- 用户反馈"上一轮刚说过的事它又忘了"，就是这个原因。

### 3.3 存储粒度太粗

当前存进 Qdrant 的是"一轮 user + assistant 文本"（`rag/retriever.py:97-103`）：

```python
payload={
    "conv_id": conv_id,
    "user": user_msg,
    "assistant": assistant_msg,
    "msg_idx": user_idx,
},
```

这对"相关对话回忆"有用，但**不适合做偏好、约束、身份、任务状态**。

对比 Mem0：把记忆拆成 **事实条目**（fact），并区分 factual / episodic / semantic / working 四种类型，每条有自己的 lifecycle。

ChatFlow 现在是"把整段对话 blob 丢进去"，检索回来也是一整段对话文本——等于**把旧聊天再喂一遍模型**（见下文 3.5）。

### 3.4 没有更新 / 删除 / 冲突解决

Qdrant 里的记忆是**追加式**的，不存在：

- 用户改口了 → 旧偏好作废
- 事实被纠正 → 旧事实覆盖
- 过期任务 → 自动清理

Mem0 把 `ADD / UPDATE / DELETE / NONE` 当一等更新语义。
ChatFlow 目前只有 `delete_by_conv(conv_id)`（`rag/retriever.py:170`），要么全删，要么全留。

### 3.5 检索结果原样注入，不是结构化注入

`memory/context_builder.py:68-74`：

```python
cleaned_memories = _deduplicate_memories(long_term_memories or [], ...)
if cleaned_memories:
    memories_text = "\n\n".join(cleaned_memories)
    system_parts.append(
        "\n【长期记忆】\n"
        "以下是与当前问题高度相关的历史对话记录，请参考这些内容来回答：\n"
        f"{memories_text}"
    )
```

召回的内容直接拼进 system prompt，本质上是**再喂一遍旧对话**。

成熟做法会分层：

- **Core memory**（总是可见）：用户身份、长期偏好、当前任务状态
- **Retrieved facts**（按需召回）：结构化的小事实条目
- **Working scratchpad**（本轮可写）：agent 自己的笔记
- **Archival passages**（大块上下文）：完整历史对话

ChatFlow 现在只有第 4 档，没前三档。

### 3.6 去重/相关性策略还比较粗

两个相关函数：

- `_deduplicate_memories`（`memory/context_builder.py:132`）：词重叠率 ≥ 55% 就过滤。
- 忘记模式（`rag/retriever.py:141`）：query vs summary/recent 的 embedding 余弦相似度。

比拍脑袋强，但少了：

- **Importance**（重要偏好 > 闲聊）
- **Recency**（最近说的优先）
- **Source reliability**（用户明确声明 > 模型推断）
- **Memory type**（偏好 / 事实 / 任务状态各有权重）
- **Conflict resolution**（两条矛盾记忆谁胜出）

### 3.7 缺"核心常驻记忆"和"工作记忆"分离

现在的三档：

1. 滑动窗口（最近 N 轮）
2. `mid_term_summary`（压缩摘要）
3. Qdrant 向量记忆（按 query 召回）

缺的第四档：**总是在上下文里的核心块**。

至少应该有：

- `user_profile`：身份、工作角色、偏好语言
- `project_rules`：当前仓库/工作目录的长期规范
- `current_task`：正在进行的长期任务、卡点
- `learned_preferences`：被确认过的风格/技术栈偏好

这些东西不适合靠 RAG 召回——它们应该永远可见。

---

## 4. Agent 图层的痛点（补充）

虽然你主要问的是 prompt / memory，但顺着看代码发现图结构本身也在放大这些问题。

### 4.1 节点文件越来越胖

```
graph/nodes/call_model_node.py         481 行
graph/nodes/planner_node.py            521 行
graph/nodes/reflector_node.py          419 行
graph/nodes/save_response_node.py      419 行
graph/nodes/call_model_after_tool_node.py  370 行
```

`call_model_node.py` 里已经塞了：

- 网页澄清的正则预检（`:34`）
- 步骤 0 消息构建（`:165`）
- 步骤 1+ 聚焦消息构建（`:207`）
- 视觉路径（`:311`）
- LLM 路径、流式处理、工具调用收集……

**Prompt 编排逻辑和节点执行逻辑混在一起**，改一条 prompt 经常要动代码。

### 4.2 上下文"隔离"靠手工重写消息

`call_model_node._build_focused_step_messages` 的核心做法是：丢掉 `state["messages"]`，用步骤结果手工拼一份新的消息列表。

这说明一个根本问题：**GraphState 没有分清"对话历史"和"当前任务工作区"**。
所以节点只能靠手动裁剪来模拟"工作记忆"。

修法方向应该是：工作记忆是 state 的一等字段（task context / scratchpad），而不是每个节点各自重新组装一遍 messages。

### 4.3 硬编码关键词做路由兜底

`call_model_node.py:34-40`:

```python
_WEBPAGE_KEYWORDS = re.compile(r"(网页|页面|html|h5|落地页|官网|网站|ui\s*界面|前端界面|web\s*页)", re.IGNORECASE)
_STYLE_KEYWORDS = re.compile(r"(风格|色调|主题色|配色|设计风格|...)", re.IGNORECASE)
```

正则兜底本身不是错，问题在于它**和模型层协议并行存在**：
- 模型输出 `[NEED_CLARIFICATION]`（prompt 层协议）
- 代码用正则预检（代码层兜底）

两套系统做同一件事，改任何一侧都可能让另一侧失效。

---

## 5. 和主流开源 Agent 的结构差距

| 维度 | ChatFlow 现状 | OpenHands | Letta | Mem0 / AutoGen |
| --- | --- | --- | --- | --- |
| Prompt 分层 | system + node + tool guidance 同层 | AGENTS.md（常驻）+ skills（触发）+ skill body（按需） | core memory blocks 分层 | — |
| 工具说明注入 | 全量拼入 system | keyword trigger + progressive disclosure | — | — |
| 上下文预算 | 不管理 | context condenser 主动压缩 | 明确层级调度 | — |
| 记忆作用域 | 单会话 | repo 级 + 会话级 | user / agent / block 多级 | user_id / agent_id / run_id |
| 记忆粒度 | Q&A 文本对 | skill 文档 + 对话摘要 | memory blocks + archival passages | 结构化事实条目 |
| 记忆更新 | 追加 + 按会话删 | — | agent 可编辑 core memory | ADD/UPDATE/DELETE/NONE |
| 记忆检索 | 向量 + 词重叠去重 | context condenser | 分层访问 | 带 importance / recency |
| 核心常驻记忆 | 无 | AGENTS.md | memory blocks | — |
| Sleep-time 整理 | 无 | — | sleep-time agent | — |

---

## 6. 升级到 2.0 的分阶段路线图

按"收益 / 风险"比排，建议这个顺序。

### 阶段 1（快赢，1~2 周可落）

**目标**：让 prompt 层和记忆层的**边界清晰起来**。

_进度：2026-04-20 第一阶段全部完成。_

1. **建 prompt 优先级表** — ✅ **已做**
   - `memory/context_builder.py` `build_messages` 改成显式 8 层 `layers: list[str]`，从上到下：
     1. 平台身份 + 全局规则（`DEFAULT_SYSTEM_PROMPT` + 当前日期）
     2. 项目规则（`core_memory.project_rules`，硬约束）
     3. 用户画像（`core_memory.user_profile`）
     4. 已确认偏好（`core_memory.learned_preferences`）
     5. 当前任务（`core_memory.current_task`）
     6. 对话背景摘要（`mid_term_summary`）
     7. 长期记忆（RAG 检索）
     8. 可用工具指南（放最底层，避免与规则层争抢注意力）
   - `core_memory` 不再作为单一块注入，而是按字段拆到对应层。
   - 删除了 `memory/core_memory.py` 的 `render_core_memory` / `_render_list`（渲染职责下沉到 `context_builder`）。

2. **工具 guidance 按 route 筛选** — ✅ **已做（简化版）**
   - 最初尝试"按 tag 字典做细粒度 route→tools 映射"，引入了 `_route_guidance_tags` 和工具 `TAGS` 的隐式耦合，已回退。
   - 当前实现（`tools/skill.py` `build_guidance`）：`chat` 路由不注入任何 guidance；其他路由全注入。
   - 精细可见性如有需要，改到工具绑定层做（决定哪些工具进 LLM），而不是在 guidance 层做 tag 字典。

3. **引入 `core_memory` 字段** — ✅ **已做（显式写入，非自动抽取）**
   - DB/Schema：`conversations.core_memory JSONB NOT NULL DEFAULT '{}'`（`db/migrate.py` + `db/models.py` + `memory/schema.py`）
   - 写入方式：**不做隐式抽取**。最初尝试用 regex 关键词（"我喜欢/我偏好/请默认/我是..."）自动抽，发现是把分析里批评过的 `_WEBPAGE_KEYWORDS` 反模式搬到了记忆层；已全部删除。
   - 改为 LLM 显式调用 `remember_preference(category, content)`（`tools/builtin/remember.py`），通过 `sandbox.context.current_conv_id` 拿到会话后 `memory_store.save(conv)` 持久化。
   - **缓存一致性已修**：`memory/store.py` `save()` 末尾加了 `await _notify_cache_invalidation(conv.id)`，`title` / `system_prompt` / `mid_term_summary` / `core_memory` 的跨 worker 失效一并解决。

### 阶段 2（结构性，3~4 周）

**目标**：让记忆从"对话回忆"升级为"agent memory"。

4. **写入实时化**
   - 每轮结束后异步抽取事实（单独的 extractor LLM，廉价模型即可）
   - 抽取结果直接写入 Qdrant，不再等压缩

5. **事实化粒度**
   - Qdrant payload 从 `{user, assistant}` 改成 `{fact, source, confidence, type, ts}`
   - 加 `user_id` 字段，支持跨会话召回

6. **更新 / 删除协议**
   - 每条新事实进来前先检索相近事实
   - 如果冲突 → 用 LLM 判定 ADD/UPDATE/DELETE/NONE（参考 Mem0 的 update_memory_prompt）

7. **协议化澄清**
   - 删掉字符串包 JSON 的协议
   - 改成 `request_clarification(question, items)` tool
   - 同时删掉 `call_model_node.py` 里的正则预检

### 阶段 3（可选，深水区）

**目标**：向 "上下文操作系统" 靠拢。

8. **Skill 按需加载**
   - 每个工具分成 "短描述（常驻）" + "完整使用文档（按需）"
   - 模型需要时 tool_call 一个 `read_skill(name)`
   - 类似 OpenHands 的 progressive disclosure

9. **Prompt 可评估**
   - Prompt 版本号
   - 回归样本集（固定输入 → 期望行为）
   - 改 prompt 时自动跑 diff

10. **Sleep-time 记忆整理**
    - 夜间定时任务压缩、去重、冲突解决
    - Letta 的 sleep-time agent 思路

---

## 7. 如果只能做三件事

按这个顺序做，性价比最高：

1. **Prompt 分层**：先把平台 / 项目 / 节点 / 工具四层明确分开，别再都汇总进一个 system prompt。
2. **加 core_memory 常驻块**：`user_profile` / `project_rules` / `current_task` / `learned_preferences` 四块，比做新 RAG 收益大得多。
3. **实时事实抽取 + 更新**：不要再只靠压缩写入，也不要只存 Q&A 对。

其他都可以慢慢来。这三件做完，用户就会觉得"它变聪明了"。

---

## 参考

- OpenHands context condenser: <https://docs.all-hands.dev/>
- OpenHands skills architecture (progressive disclosure)
- Letta memory hierarchy & memory blocks: <https://docs.letta.com/>
- Letta sleep-time agents
- Mem0 custom instructions / update memory prompt: <https://docs.mem0.ai/>
- AutoGen memory protocol: Microsoft AutoGen docs
