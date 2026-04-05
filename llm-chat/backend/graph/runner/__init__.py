"""
graph.runner 包：LangGraph 事件流 → FastAPI SSE 转换器

对外接口（main.py 唯一依赖）：
    stream_response(conv_id, user_message, model, temperature, ...) -> AsyncGenerator[str, None]

SSE 事件格式（供前端消费）：
    {"status": "routing"}                                  ← 路由意图分类中
    {"route": {"model": "...", "intent": "..."}}           ← 路由结果
    {"status": "planning"}                                 ← 规划中
    {"plan_generated": {"steps": [...]}}                   ← 计划生成完毕
    {"reflection": {"content": "...", "decision": "..."}}  ← 反思结果
    {"status": "thinking", "model": "..."}                 ← LLM 开始推理
    {"thinking": "...chunk..."}                            ← <think> 推理块内容（增量）
    {"content": "...token..."}                             ← LLM 输出 token（增量）
    {"tool_call": {"name": "...", "input": {...}}}         ← 工具调用开始
    {"search_item": {"url":"","title":"","status":""}}     ← web_search 单条结果
    {"tool_result": {"name": "...", ...}}                  ← 工具完成信号
    {"status": "saving"}                                   ← 保存响应中
    {"status": "cache_hit", "similarity": 0.95}           ← 缓存命中
    {"ping": true}                                         ← 心跳（防 nginx 超时）
    {"done": true, "compressed": bool}                    ← 流结束信号
    {"error": "..."}                                       ← 错误信号
"""
from graph.runner.stream import stream_response

__all__ = ["stream_response"]
