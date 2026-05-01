"""量化分析模块 — A 股选股 / 因子计算 / 多 provider 抽象。

设计要点：
  - Provider 抽象：MarketDataProvider Protocol + Registry，按能力 + 优先级 + 健康度选择
  - 数值确定性：因子计算和综合分由代码完成，LLM 只负责解释和风险提示
  - 异步友好：同步 SDK 用 asyncio.to_thread 包装，避免阻塞 FastAPI event loop
"""
