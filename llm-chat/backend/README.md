# ChatFlow 后端架构详解

## 目录

- [整体请求链路](#整体请求链路)
- [LangGraph 图结构](#langgraph-图结构)
- [asyncio 并发架构（核心优化）](#asyncio-并发架构核心优化)
- [SSE 事件处理链](#sse-事件处理链)
- [think-block 三层过滤](#think-block-三层过滤)
- [记忆系统](#记忆系统)
- [节点详解](#节点详解)
- [配置参考](#配置参考)

---

## 整体请求链路

```mermaid
sequenceDiagram
    participant B as 浏览器
    participant N as nginx
    participant F as FastAPI
    participant R as runner.py
    participant G as LangGraph

    B->>N: POST /api/chat
    N->>F: proxy_pass (timeout 600s)
    F->>R: stream_response()
    R->>R: 创建 asyncio.Queue
    R->>G: create_task(_graph_producer)
    R->>R: create_task(_heartbeat)

    loop 每 20 秒（心跳）
        R-->>N: data: {"ping":true}
        N-->>B: 心跳（重置超时计时器）
    end

    loop 图执行期间
        G-->>R: LangGraph 事件
        R-->>N: data: {SSE事件}
        N-->>B: 流式推送
    end

    R-->>N: data: {"done":true}
    N-->>B: 流结束
```

> **关键**：心跳每 20s 发一次，让 nginx 的 `proxy_read_timeout` 计时器持续重置。qwen3 推理时 `<think>` 块被过滤不发送，否则长推理时间会导致 nginx 判定连接空闲而断流。

---

## LangGraph 图结构

### 完整图（ROUTER_ENABLED=true）

```mermaid
flowchart TD
    START([START]) --> route_model

    route_model["🔀 route_model\n判断意图\nchat / code / search / search_code"]
    route_model --> retrieve_context

    retrieve_context["📚 retrieve_context\nRAG检索 + 组装历史消息"]
    retrieve_context --> planner

    planner["📋 planner\n生成执行计划\n仅 search / search_code 触发"]
    planner --> call_model

    call_model["🧠 call_model\ntool_model 调用\n绑定工具（search路由）"]

    call_model -->|有工具调用| tools
    call_model -->|无工具 + 有计划| reflector
    call_model -->|无工具 + 无计划| save_response

    tools["⚙️ tools\nToolNode\n并发执行工具"]
    tools --> call_model_after_tool

    call_model_after_tool["🧠 call_model_after_tool\nanswer_model 综合工具结果"]
    call_model_after_tool -->|还有工具调用| tools
    call_model_after_tool -->|无工具 + 有计划| reflector
    call_model_after_tool -->|无工具 + 无计划| save_response

    reflector["🔍 reflector\n评估步骤完成情况"]
    reflector -->|continue| call_model
    reflector -->|retry| call_model
    reflector -->|done| save_response

    save_response["💾 save_response\n持久化消息 + 工具摘要"]
    save_response --> compress_memory

    compress_memory["🗜️ compress_memory\n按需压缩 → 写 Qdrant"]
    compress_memory --> END([END])

    style route_model fill:#dbeafe
    style planner fill:#fef3c7
    style call_model fill:#dcfce7
    style call_model_after_tool fill:#dcfce7
    style reflector fill:#fce7f3
    style tools fill:#f3e8ff
    style save_response fill:#e0f2fe
    style compress_memory fill:#e0f2fe
```

### 递归上限计算

`recursion_limit = 60`，支持最多 **13 个计划步骤**：

```mermaid
block-beta
  columns 5
  A["固定开销\n5 节点"]:1
  B["步骤 1\n4 节点"]:1
  C["步骤 2\n4 节点"]:1
  D["···"]:1
  E["步骤 13\n4 节点"]:1
```

> `固定(5) + 每步(4) × 13步 = 57 ≤ 60`  旧版默认 25，3 步计划就可能撞墙。

### chat / code 快速路径

```mermaid
flowchart LR
    S([START]) --> RC[retrieve_context]
    RC --> PL["planner\n(跳过，返回空计划)"]
    PL --> CM[call_model]
    CM -->|无工具| SR[save_response]
    CM -->|有工具| T[tools]
    T --> CMT[call_model_after_tool]
    CMT --> SR
    SR --> CMP[compress_memory]
    CMP --> E([END])
```

---

## asyncio 并发架构（核心优化）

### 旧版 vs 新版对比

```mermaid
flowchart LR
    subgraph OLD["❌ 旧版：串行"]
        direction TB
        O1["graph.astream_events()"] --> O2["yield chunk"]
        O2 --> O3["nginx 等待..."]
        O3 --> O4["300s 无数据 → 断流"]
    end

    subgraph NEW["✅ 新版：并发队列+心跳"]
        direction TB
        N1["_graph_producer Task"]
        N2["_heartbeat Task\n每 20s"]
        N3["asyncio.Queue"]
        N4["主循环\nawait queue.get()"]
        N1 -->|事件| N3
        N2 -->|ping| N3
        N3 --> N4
        N4 -->|yield| N5["nginx ← 持续有数据"]
    end
```

### asyncio 三个角色详解

```mermaid
flowchart TD
    subgraph EL["Python 事件循环 Event Loop"]
        direction TB

        subgraph TA["Task A: _graph_producer"]
            A1["graph.astream_events()"]
            A2["await queue.put(event)"]
            A1 --> A2
        end

        subgraph TB2["Task B: _heartbeat"]
            B1["await asyncio.sleep(20)"]
            B2["await queue.put(ping)"]
            B1 --> B2 --> B1
        end

        subgraph TC["主协程（生成器）"]
            C1["await queue.get()"]
            C2{"kind?"}
            C3["yield SSE chunk"]
            C4["yield ping"]
            C5["break"]
            C1 --> C2
            C2 -->|event| C3 --> C1
            C2 -->|ping| C4 --> C1
            C2 -->|done| C5
        end

        TA -->|"put('event', ...)"| Q[(asyncio.Queue)]
        TB2 -->|"put('ping', None)"| Q
        Q -->|"get()"| TC
    end
```

### 客户端断开时的清理流程

```mermaid
sequenceDiagram
    participant C as 客户端
    participant F as FastAPI
    participant M as 主协程
    participant A as Task A (图执行)
    participant B as Task B (心跳)

    C->>F: 断开连接
    F->>M: 抛出 CancelledError
    M->>M: except CancelledError\ngraph_done = False
    M->>A: task.cancel()
    M->>B: task.cancel() [finally]
    Note over M: graph_done=False\n不发 done 事件\n生成器正常退出
    A->>A: CancelledError → pass\n队列放 ("done", None)\n[无人消费，GC 回收]
```

### 连接超时防线（多层）

```mermaid
flowchart LR
    HB["心跳\n每 20s 发 ping"] -->|防线1| NG["nginx\nproxy_read_timeout\n600s"]
    NG -->|防线2| FE["前端 streamDone 标志\n流关闭未收 done\n→ onStopped()"]
    FE -->|防线3| UI["UI loading\n必然被清除"]

    style HB fill:#dcfce7
    style NG fill:#fef3c7
    style FE fill:#dbeafe
    style UI fill:#f3e8ff
```

---

### stream_response 完整数据流

> 这是 `runner.py` 里最核心的函数，把 LangGraph 的事件流翻译成 SSE 字符串流发给浏览器。下面逐阶段拆解它的执行过程。

#### 阶段一：启动（函数入口）

```mermaid
sequenceDiagram
    participant FA as FastAPI
    participant SR as stream_response()
    participant Q  as asyncio.Queue
    participant GA as Task A _graph_producer
    participant HB as Task B _heartbeat

    FA->>SR: 调用 stream_response(conv_id, message, model, ...)
    SR->>SR: 构建 initial_state（GraphState 字典）
    SR->>Q:  创建空队列
    SR->>GA: asyncio.create_task(_graph_producer)<br/>立即放入事件循环，不等待
    SR->>HB: asyncio.create_task(_heartbeat)<br/>立即放入事件循环，不等待
    Note over GA,HB: 两个 Task 与主协程并发运行<br/>各自独立，通过 Queue 通信
    SR->>SR: 进入 while True 主循环<br/>await queue.get() 挂起等待
```

此刻三条执行流同时存在于事件循环中，谁有数据谁先跑。

---

#### 阶段二：正常执行期间的数据流

```mermaid
flowchart TD
    subgraph TaskA["Task A: _graph_producer（后台）"]
        A1["graph.astream_events(initial_state,\n  version='v2',\n  config={'recursion_limit':60})"]
        A2["每产生一个 LangGraph 事件"]
        A3["await queue.put(('event', event))"]
        A1 --> A2 --> A3 --> A2
    end

    subgraph TaskB["Task B: _heartbeat（后台）"]
        B1["await asyncio.sleep(20)"]
        B2["await queue.put(('ping', None))"]
        B1 --> B2 --> B1
    end

    subgraph MAIN["主协程（while True 循环）"]
        M1["kind, val = await queue.get()"]
        M2{"kind 是什么?"}
        M3["async for chunk in\n_dispatcher.dispatch(val, ctx)\n  yield chunk"]
        M4["yield _sse({'ping':True})"]
        M5["yield _sse({'error':str(val)})"]
        M6["graph_done = True\nbreak"]
        M1 --> M2
        M2 -->|event| M3 --> M1
        M2 -->|ping| M4 --> M1
        M2 -->|error| M5 --> M1
        M2 -->|done| M6
    end

    TaskA -->|put| Q[(asyncio.Queue\n无限缓冲)]
    TaskB -->|put| Q
    Q -->|get| MAIN

    MAIN -->|"yield 'data:{...}\n\n'"| NGINX["nginx → 浏览器"]
```

**关键细节**：
- `await queue.get()` 挂起时，事件循环切走执行 Task A / Task B
- Task A 的 `await queue.put()` 完成后，事件循环切回主协程继续 `get()`
- `yield chunk` 把 SSE 字符串交给 FastAPI 的 `StreamingResponse`，FastAPI 负责实际发送给 nginx

---

#### 阶段三：一个 LangGraph 事件如何变成 SSE 字符串

```mermaid
flowchart LR
    EV["LangGraph 原始事件\n{\n  event: 'on_chat_model_stream',\n  name: 'ChatOllama',\n  metadata: {langgraph_node: 'call_model'},\n  data: {chunk: AIMessageChunk(content='你好')}\n}"]

    EV --> DISP["EventDispatcher.dispatch(event, ctx)"]
    DISP --> MATCH["按 event_type + node_name\n顺序匹配 Handler 列表\n命中 LLMStreamHandler"]
    MATCH --> HANDLER["LLMStreamHandler.handle()\n1. 取 chunk.content = '你好'\n2. 状态机过滤 think 块\n3. filtered = '你好'"]
    HANDLER --> SSE["yield _sse({'content':'你好'})\n↓\n'data: {\"content\":\"你好\"}\n\n'"]
    SSE --> Q2["queue.put(('event', ...))\n↑ 这个字符串经过 queue\n最终 yield 给 FastAPI"]
```

---

#### 阶段四：正常结束

```mermaid
sequenceDiagram
    participant GA as Task A
    participant Q  as Queue
    participant M  as 主循环
    participant FA as FastAPI

    GA->>GA: graph.astream_events() 迭代完毕
    GA->>Q:  put(("done", None))   ← finally 块保证一定执行
    GA->>GA: Task A 自然结束

    M->>Q:  await queue.get()
    Q-->>M: ("done", None)
    M->>M:  graph_done = True; break
    Note over M: 退出 while True
    M->>M:  finally: hb_task.cancel(), graph_task.cancel()
    Note over M: graph_done=True
    M->>FA: yield _sse({"done":True, "compressed":...})
    FA->>FA: StreamingResponse 结束，关闭 HTTP 连接
```

**`graph_done` 标志的作用**：区分「正常结束」和「异常断开」。只有正常收到 `("done", None)` 才发 `{"done":true}` 给前端，客户端断开走 `CancelledError` 时 `graph_done=False`，不发。

---

#### 阶段五：异常——图执行报错

```mermaid
sequenceDiagram
    participant GA as Task A
    participant Q  as Queue
    participant M  as 主循环
    participant FE as 前端

    GA->>GA: graph.astream_events() 抛出异常<br/>（如 GraphRecursionError）
    GA->>GA: except Exception as exc: 捕获
    GA->>Q:  put(("error", exc))
    GA->>Q:  put(("done", None))   ← finally 块

    M->>Q:  get() → ("error", exc)
    M->>FE: yield _sse({"error":"Recursion limit..."})
    Note over FE: 前端 onChunk 追加 ⚠️ 错误提示到消息末尾
    M->>Q:  get() → ("done", None)
    M->>M:  graph_done = True; break
    M->>FE: yield _sse({"done":true})
    Note over FE: loading 清除，用户看到错误提示
```

---

#### 阶段六：异常——客户端主动断开

```mermaid
sequenceDiagram
    participant FE as 前端（用户关闭页面）
    participant FA as FastAPI
    participant M  as 主循环
    participant GA as Task A（正在跑图）
    participant HB as Task B（心跳）

    FE->>FA: TCP 连接断开
    FA->>M:  向生成器 athrow(CancelledError)
    M->>M:   await queue.get() 处被打断
    M->>M:   except CancelledError:\n  graph_done = False

    M->>GA:  graph_task.cancel()
    M->>HB:  hb_task.cancel()  ← finally 块

    GA->>GA: CancelledError 在 astream_events 内部传播
    GA->>GA: except CancelledError: pass
    GA->>GA: finally: queue.put(("done", None))\n[无人消费，随 Queue GC 回收]

    Note over M: graph_done=False\n不执行 yield done\n生成器退出
    Note over FA: StreamingResponse 感知到生成器退出\n连接关闭处理完毕
```

---

#### 状态机总览

```mermaid
stateDiagram-v2
    [*] --> 启动: FastAPI 调用 stream_response()

    启动 --> 运行中: create_task×2\n进入 while True

    state 运行中 {
        [*] --> 等待: await queue.get()
        等待 --> 处理事件: kind=event
        等待 --> 发心跳: kind=ping
        等待 --> 发错误: kind=error
        等待 --> 准备结束: kind=done
        处理事件 --> 等待: yield SSE chunk
        发心跳 --> 等待: yield ping
        发错误 --> 等待: yield error SSE
        准备结束 --> [*]: graph_done=True, break
    }

    运行中 --> 正常结束: graph_done=True\nfinally cancel tasks
    运行中 --> 异常断开: CancelledError\ngraph_done=False\nfinally cancel tasks

    正常结束 --> [*]: yield {"done":true}
    异常断开 --> [*]: 不发 done，静默退出
```

---

## SSE 事件处理链

```mermaid
flowchart TD
    EV["LangGraph astream_events 事件\nevent_type + node_name + data"]

    EV --> D{EventDispatcher\n顺序匹配}

    D -->|on_chain_start + route_model| H1["RouteStartHandler"]
    D -->|on_chain_end + route_model| H2["RouteEndHandler"]
    D -->|on_chain_start + planner| H3["PlannerStartHandler"]
    D -->|on_chain_end + planner| H4["PlannerEndHandler"]
    D -->|on_chain_end + reflector| H5["ReflectorEndHandler"]
    D -->|on_chat_model_start + call_model*| H6["LLMStartHandler"]
    D -->|on_chat_model_stream + call_model*| H7["LLMStreamHandler\n⚠️ 过滤 think 块"]
    D -->|on_tool_start| H8["ToolStartHandler"]
    D -->|on_tool_end| H9["ToolEndHandler"]
    D -->|on_chain_end + compress_memory| H10["CompressEndHandler\n仅更新 ctx，不发 SSE"]

    H1 --> S1["{'status':'routing'}"]
    H2 --> S2["{'route':{'model':...,'intent':...}}"]
    H3 --> S3["{'status':'planning'}"]
    H4 --> S4["{'plan_generated':{'steps':[...]}}"]
    H5 --> S5["{'plan_generated'} + {'reflection':...}"]
    H6 --> S6["{'status':'thinking','model':'...'}"]
    H7 --> S7["{'content':'...token...'}"]
    H8 --> S8["{'tool_call':{'name':...,'input':...}}"]
    H9 --> TF{"ToolResultFormatter"}
    TF -->|web_search| S9a["{'search_item':...} × N条"]
    TF -->|fetch_webpage| S9b["{'tool_result':{'status':'done|fail'}}"]
    TF -->|其他| S9c["{'tool_result':{'output':'...'}}"]
```

---

## think-block 三层过滤

qwen3 在 search/planning 模式下输出大量 `<think>...</think>` 推理内容，如果不过滤会导致 marked.js 误判代码块为文本（代码块没有预览按钮）。

```mermaid
flowchart LR
    subgraph L1["层 1：流式过滤\nrunner.py LLMStreamHandler"]
        direction TB
        T1["token chunk 到达"]
        T2{"in_think_block?"}
        T3["丢弃，找 </think>"]
        T4["保留，找 <think>"]
        T5["yield {'content':'...'}"]
        T1 --> T2
        T2 -->|是| T3 --> T2
        T2 -->|否| T4 --> T5
    end

    subgraph L2["层 2：存储过滤\nnodes.py save_response"]
        direction TB
        S1["full_response"]
        S2["re.sub(think块)"]
        S3["写入 PostgreSQL"]
        S1 --> S2 --> S3
    end

    subgraph L3["层 3：渲染过滤\nMessageItem.vue"]
        direction TB
        V1["message.content"]
        V2[".replace(think块正则)"]
        V3["markedInstance.parse()"]
        V4["代码块有预览按钮 ✓"]
        V1 --> V2 --> V3 --> V4
    end

    L1 -->|跨chunk状态: ctx.in_think_block| L2
    L2 -->|数据库干净存储| L3
```

> **为什么要三层**：流式过滤基于 chunk 边界，边界刚好在标签中间时会有遗漏；存储过滤保证数据库干净；渲染过滤在浏览器端兜底，防止历史消息加载时残留块破坏 markdown 解析。

---

## 记忆系统

```mermaid
flowchart TD
    subgraph PG["PostgreSQL（短期/结构化）"]
        P1[conversations 表]
        P2[messages 表\n完整对话历史]
        P3[tool_events 表\n工具调用记录]
    end

    subgraph QD["Qdrant（长期/向量）"]
        Q1["向量集合\nper conv_id"]
        Q2["embedding\n摘要/消息片段"]
        Q3["cos_similarity\n相关性检索"]
    end

    subgraph RC["retrieve_context 节点"]
        R1["向量检索长期记忆"]
        R2{"检索到\n相关记忆?"}
        R3["forget_mode=false\n带入历史上下文"]
        R4{"新问题与\n近期话题相关?"}
        R5["forget_mode=true\n跳过旧上下文"]
        R1 --> R2
        R2 -->|是| R3
        R2 -->|否| R4
        R4 -->|是| R3
        R4 -->|否| R5
    end

    subgraph CM["compress_memory 节点"]
        C1{"消息数\n超过阈值?"}
        C2["SUMMARY_MODEL\n生成摘要"]
        C3["批量写入 Qdrant"]
        C4["旧消息留 PG\n不再送入上下文"]
        C1 -->|是| C2 --> C3 --> C4
        C1 -->|否| C5["跳过"]
    end

    QD -->|检索| RC
    PG -->|加载历史| RC
    RC -->|组装 messages| LLM["LLM 推理"]
    LLM -->|对话结束| PG
    PG -->|触发| CM
    CM -->|长期记忆| QD
```

---

## 节点详解

### 路由决策表

| route | 触发场景 | tool_model | answer_model |
|---|---|---|---|
| `chat` | 通用聊天、推理、翻译、写作 | = answer_model | CHAT_MODEL |
| `code` | 纯代码编写/调试，需求明确 | = answer_model | CODE_MODEL |
| `search` | 需联网查实时信息，不写代码 | SEARCH_MODEL | SEARCH_MODEL |
| `search_code` | 查文档/仓库后再写代码 | SEARCH_MODEL | CODE_MODEL |

### reflector 决策逻辑

```mermaid
flowchart TD
    IN["reflector 收到状态"]
    A{"current_idx >= total\n或 step_iters >= 2?"}
    B{"是最后一步\n且有响应?"}
    C["LLM 评估\n最近5条消息"]
    D{"LLM 决策"}

    IN --> A
    A -->|是| DONE["done\n强制完成（安全边界）"]
    A -->|否| B
    B -->|是| DONE
    B -->|否| C
    C --> D
    D -->|done| DONE
    D -->|continue| CONT["continue\n下一步 index++\n注入步骤指令 HumanMessage"]
    D -->|retry| RETRY["retry\nstep_iterations++\n重试当前步骤（最多2次）"]
    D -->|解析失败| DONE

    DONE --> SR["→ save_response"]
    CONT --> CM2["→ call_model"]
    RETRY --> CM2

    style DONE fill:#dcfce7
    style CONT fill:#dbeafe
    style RETRY fill:#fef3c7
```

---

## 配置参考

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `CHAT_MODEL` | qwen3:8b | chat 路由 answer_model |
| `ROUTER_MODEL` | qwen3:8b | route_model 节点，temperature=0 |
| `SEARCH_MODEL` | qwen3:8b | search 路由 tool_model |
| `SUMMARY_MODEL` | qwen3:8b | compress_memory 摘要生成 |
| `EMBEDDING_MODEL` | bge-m3 | Qdrant 向量化 |
| `ROUTER_ENABLED` | true | 是否启用 route_model 节点 |
| `LONGTERM_MEMORY_ENABLED` | true | 是否启用 Qdrant 长期记忆 |
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` | Ollama OpenAI 兼容 API |
| `recursion_limit` | 60（硬编码） | LangGraph 节点执行上限，支持 13 步计划 |
