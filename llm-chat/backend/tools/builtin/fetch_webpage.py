"""
内置工具：读取网页正文内容
依赖：httpx（已在 requirements.txt 中）

重要：使用同步 httpx.Client 通过 asyncio.to_thread 执行，不阻塞 event loop。
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = (
    "搜索给你摘要，这个工具给你原文。"
    "当摘要不够、需要核对细节、或要引述某段原话时召唤。"
    "只处理 HTML 页面；PDF / 图片 / 视频等二进制文件会被跳过。"
)
ERROR_HINT = "网页读取失败可能是目标站点限制，可尝试其他 URL 或换用 web_search 搜索相关内容。"
TAGS = ["search", "web"]
DISPLAY_MODE = "default"
import asyncio
import logging
import re
import httpx
from langchain_core.tools import tool

logger = logging.getLogger("tools.fetch_webpage")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _strip_html(html: str) -> str:
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


_BINARY_CONTENT_TYPES = (
    "image/", "video/", "audio/",
    "application/octet-stream", "application/pdf",
    "application/zip", "application/x-", "font/",
)


def _do_fetch(url: str) -> str:
    """同步抓取，在线程池中执行。"""
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        resp = client.get(url, headers=_HEADERS)
        resp.raise_for_status()

    # 二进制资源（图片/视频/PDF 等）不可解析为文本，直接返回说明
    content_type = resp.headers.get("content-type", "").lower().split(";")[0].strip()
    if any(content_type.startswith(bt) for bt in _BINARY_CONTENT_TYPES):
        logger.info("fetch_webpage 跳过二进制 | content-type=%s | url='%s'", content_type, url)
        return f"该 URL 指向的是二进制文件（{content_type}），无法提取文字内容。"

    text = _strip_html(resp.text)
    if len(text) > 3000:
        text = text[:3000] + "\n\n[内容已截断...]"
    return text or "页面内容为空"


@tool
async def fetch_webpage(url: str) -> str:
    """
    读取某个 URL 的完整正文——搜索只给摘要，这里给原文。

    何时召唤：需要阅读完整文章、核对具体数据、引述原话、验证搜索摘要。
    只处理 HTML；PDF / 图片 / 视频等二进制无法解析。

    Args:
        url: 要读取的网页地址

    Returns:
        网页正文（纯文本，最多 3000 字）
    """
    logger.info("fetch_webpage 开始 | url='%s'", url)
    try:
        # to_thread 避免阻塞 event loop
        result = await asyncio.to_thread(_do_fetch, url)
        logger.info("fetch_webpage 完成 | url='%s' | 字符数=%d", url, len(result))
        return result
    except httpx.TimeoutException:
        logger.warning("fetch_webpage 超时 | url='%s'", url)
        return f"读取超时：{url}"
    except httpx.HTTPStatusError as e:
        logger.warning("fetch_webpage HTTP错误 | url='%s' | status=%d", url, e.response.status_code)
        return f"HTTP 错误 {e.response.status_code}：{url}"
    except Exception as exc:
        logger.error("fetch_webpage 异常 | url='%s' | error=%s", url, exc, exc_info=True)
        return f"读取失败：{exc}"
