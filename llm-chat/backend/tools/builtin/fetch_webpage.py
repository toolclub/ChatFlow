"""
内置工具：读取网页正文内容
依赖：httpx（已在 requirements.txt 中）
"""
import re
import httpx
from langchain_core.tools import tool


def _strip_html(html: str) -> str:
    """简单去除 HTML 标签，保留可读文本。"""
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


@tool
def fetch_webpage(url: str) -> str:
    """
    读取指定 URL 的网页正文内容，用于深入了解搜索结果中某个页面的详细信息。
    适用于：需要阅读完整文章、获取详细数据、验证搜索摘要时。

    Args:
        url: 要读取的网页地址

    Returns:
        网页的纯文本正文（最多 3000 字）
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()

        text = _strip_html(resp.text)
        # 截取前 3000 字，避免 token 爆炸
        if len(text) > 3000:
            text = text[:3000] + "\n\n[内容已截断...]"
        return text or "页面内容为空"

    except httpx.TimeoutException:
        return f"读取超时：{url}"
    except httpx.HTTPStatusError as e:
        return f"HTTP 错误 {e.response.status_code}：{url}"
    except Exception as exc:
        return f"读取失败：{exc}"
