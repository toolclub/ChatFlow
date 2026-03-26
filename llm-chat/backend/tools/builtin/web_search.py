"""
内置工具：网络搜索（DuckDuckGo，无需 API Key，支持中文区域）
依赖：pip install ddgs
"""
import json
from langchain_core.tools import tool


@tool
def web_search(query: str, max_results: int = 6) -> str:
    """
    搜索互联网获取最新信息。搜索词请使用中文，以获得更好的中文结果。
    适用于：查询时事新闻、最新价格、实时数据、不在训练集中的信息。

    Args:
        query:       搜索关键词（请用中文）
        max_results: 返回结果数量（默认6，最多10）

    Returns:
        JSON 格式的搜索结果列表，每项包含 title、url、snippet
    """
    try:
        from ddgs import DDGS

        max_results = min(max_results, 10)
        with DDGS() as ddgs:
            raw = list(ddgs.text(
                query,
                max_results=max_results,
                region="cn-zh",      # 中文区域，优先返回中文结果
            ))

        if not raw:
            # 降级：去掉区域限制再试一次
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))

        if not raw:
            return json.dumps(
                [{"title": "无结果", "url": "", "snippet": f"未找到「{query}」的相关信息，请换一个搜索词试试。"}],
                ensure_ascii=False,
            )

        items = [
            {
                "title":   r.get("title", ""),
                "url":     r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in raw
        ]
        return json.dumps(items, ensure_ascii=False)

    except ImportError:
        return json.dumps(
            [{"title": "缺少依赖", "url": "", "snippet": "请安装：pip install ddgs"}],
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            [{"title": "搜索失败", "url": "", "snippet": str(exc)}],
            ensure_ascii=False,
        )
