"""
内置工具：网络搜索（Tavily）
纯异步 httpx 调用，不阻塞 event loop。
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = (
    "它把你带到训练集之外的「当下的世界」。"
    "当你从记忆里答不出、不确定、或信息时效敏感（新闻 / 价格 / 天气 / 版本号 / 你不熟悉的产品或技术）时召唤。"
    "拿到结果后只基于返回内容作答，不拿自己的猜测修饰它。"
)
ERROR_HINT = "搜索失败可能是网络问题，可换关键词重试，或用 fetch_webpage 直接访问已知 URL。"
TAGS = ["search", "realtime"]
DISPLAY_MODE = "default"
import json
import logging
import os
import asyncio
import httpx
from langchain_core.tools import tool

logger = logging.getLogger("tools.web_search")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"

_RETRY_TIMES = 3      # 最大重试次数
_RETRY_DELAY = 1.0    # 初始等待秒数（指数退避：1s → 2s → 4s）


@tool
async def web_search(query: str, max_results: int = 6) -> str:
    """
    向互联网发一次查询——把你带到训练截止之后的"当下世界"。

    何时召唤：记忆里答不出、不确定、或信息时效敏感的具体事实。
    中文查询用中文关键词以获得更好结果；回来后只基于返回内容作答，不补猜测。

    Args:
        query:       搜索关键词
        max_results: 返回结果数量（默认 6，最多 10）

    Returns:
        JSON 格式的搜索结果列表，每项包含 title、url、snippet
    """
    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY 未配置")
        return _err_json("配置错误", "TAVILY_API_KEY 环境变量未设置，无法执行搜索。")

    logger.info("web_search 开始 | query='%s' max_results=%d", query, max_results)
    max_results = min(max(1, max_results), 10)

    last_exc: Exception | None = None

    for attempt in range(1, _RETRY_TIMES + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    TAVILY_URL,
                    json={
                        "api_key": TAVILY_API_KEY,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            items = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                }
                for r in data.get("results", [])
            ]

            if items:
                logger.info("web_search 完成 | query='%s' | 结果数=%d", query, len(items))
                return json.dumps(items, ensure_ascii=False)

            logger.warning("web_search 无结果 | query='%s'", query)
            return _err_json("无结果", f"未找到「{query}」的相关信息。")

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning(
                "web_search 连接失败（第%d/%d次）| query='%s' | %s",
                attempt, _RETRY_TIMES, query, exc,
            )
            if attempt < _RETRY_TIMES:
                wait = _RETRY_DELAY * (2 ** (attempt - 1))
                logger.info("web_search 等待 %.1f 秒后重试...", wait)
                await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            logger.error("web_search HTTP错误 | query='%s' | status=%d", query, exc.response.status_code)
            return _err_json(
                "请求失败",
                f"Tavily 返回 HTTP {exc.response.status_code}，请检查 API Key 或稍后重试。",
            )

        except Exception as exc:
            logger.error("web_search 未知异常 | query='%s' | %s", query, exc, exc_info=True)
            return _err_json("搜索失败", str(exc))

    logger.error("web_search 重试耗尽 | query='%s' | last_error=%s", query, last_exc)
    return _err_json("连接失败", f"连接 Tavily 失败（已重试 {_RETRY_TIMES} 次），请检查网络后重试。")


def _err_json(title: str, snippet: str) -> str:
    return json.dumps([{"title": title, "url": "", "snippet": snippet}], ensure_ascii=False)