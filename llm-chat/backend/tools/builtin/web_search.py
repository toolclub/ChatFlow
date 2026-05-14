"""
内置工具：网络搜索（Tavily）

业界范式（参考 Tavily 官方最佳实践 + Perplexity / OpenAI Deep Research）：
  1. 单次"喂饱"：basic 模式 + chunks_per_source + include_raw_content + include_answer，
     一次拿到综合答案 + 多块相关片段 + 原文，减少模型"信息不够"的错觉。
  2. 预算 + 去重：本轮内最多 _MAX_CALLS_PER_TURN 次；同 query 直接走缓存，
     超额时返回降级 note，强制模型基于已有信息收敛。
"""

# ── Skill 元数据（SkillRegistry 自动收集） ──
GUIDANCE = (
    "它把你带到训练集之外的「当下的世界」。"
    "当你从记忆里答不出、不确定、或信息时效敏感（新闻 / 价格 / 天气 / 版本号 / 你不熟悉的产品或技术）时召唤。"
    "返回结果含 answer（Tavily 综合答案）+ results（每条带 chunks 多段原文片段）。"
    "看到 answer 字段优先采纳；不同源措辞差异是常态，不要因此反复搜索。"
    "本轮调用次数有限（默认 5 次），同 query 重复会命中缓存，请一次性想清楚关键词。"
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

_RETRY_TIMES = 3        # 最大重试次数
_RETRY_DELAY = 1.0      # 初始等待秒数（指数退避：1s → 2s → 4s）
_MAX_CALLS_PER_TURN = 5  # 单轮（assistant_message）最多搜索次数；超额返回降级提示


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
        JSON 字符串，结构：
          {
            "answer": "Tavily 综合答案（可能为空）",
            "results": [
              {"title": "...", "url": "...", "snippet": "...", "chunks": ["...", ...]},
              ...
            ],
            "note": "可选的状态说明（如缓存命中 / 预算耗尽）"
          }
    """
    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY 未配置")
        return _err_payload("配置错误", "TAVILY_API_KEY 环境变量未设置，无法执行搜索。")

    scope = _budget_scope()
    query_norm = query.strip()

    # ── 1. 同 query 去重：本轮已搜过则走缓存 ─────────────────────────────────
    cached = await _maybe_get_cached(scope, query_norm)
    if cached is not None:
        logger.info("web_search 命中本轮缓存 | scope=%s | query='%s'", scope, query_norm)
        return _attach_note(cached, f"本轮已搜索过该 query，下方为上次结果。请综合现有信息作答，不要再次搜索同一关键词。")

    # ── 2. 预算检查：超额直接返回降级提示，不调 Tavily ───────────────────────
    count = await _incr_count(scope)
    if count is not None and count > _MAX_CALLS_PER_TURN:
        logger.warning(
            "web_search 超出本轮预算 | scope=%s | count=%d/%d | query='%s'",
            scope, count, _MAX_CALLS_PER_TURN, query_norm,
        )
        return _budget_exhausted_payload(query_norm, count)

    logger.info(
        "web_search 开始 | scope=%s | call=%s/%d | query='%s' | max_results=%d",
        scope, count if count is not None else "?", _MAX_CALLS_PER_TURN,
        query_norm, max_results,
    )
    max_results = min(max(1, max_results), 10)

    last_exc: Exception | None = None

    for attempt in range(1, _RETRY_TIMES + 1):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    TAVILY_URL,
                    json={
                        "api_key": TAVILY_API_KEY,
                        "query": query_norm,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "chunks_per_source": 3,
                        "include_raw_content": True,
                        "include_answer": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            answer = data.get("answer") or ""
            results_raw = data.get("results", []) or []
            results = [_normalize_result(r) for r in results_raw]

            if not results and not answer:
                logger.warning("web_search 无结果 | query='%s'", query_norm)
                payload = _err_payload("无结果", f"未找到「{query_norm}」的相关信息。")
                await _maybe_set_cache(scope, query_norm, payload)
                return payload

            payload = json.dumps(
                {"answer": answer, "results": results},
                ensure_ascii=False,
            )
            await _maybe_set_cache(scope, query_norm, payload)

            logger.info(
                "web_search 完成 | query='%s' | answer_len=%d | results=%d",
                query_norm, len(answer), len(results),
            )
            return payload

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning(
                "web_search 连接失败（第%d/%d次）| query='%s' | %s",
                attempt, _RETRY_TIMES, query_norm, exc,
            )
            if attempt < _RETRY_TIMES:
                wait = _RETRY_DELAY * (2 ** (attempt - 1))
                logger.info("web_search 等待 %.1f 秒后重试...", wait)
                await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            logger.error("web_search HTTP错误 | query='%s' | status=%d", query_norm, exc.response.status_code)
            return _err_payload(
                "请求失败",
                f"Tavily 返回 HTTP {exc.response.status_code}，请检查 API Key 或稍后重试。",
            )

        except Exception as exc:
            logger.error("web_search 未知异常 | query='%s' | %s", query_norm, exc, exc_info=True)
            return _err_payload("搜索失败", str(exc))

    logger.error("web_search 重试耗尽 | query='%s' | last_error=%s", query_norm, last_exc)
    return _err_payload("连接失败", f"连接 Tavily 失败（已重试 {_RETRY_TIMES} 次），请检查网络后重试。")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_result(r: dict) -> dict:
    """统一 Tavily 单条结果格式。chunks 由 raw_content 分段填充作为兜底。"""
    raw_content = r.get("raw_content") or ""
    # Tavily advanced 模式下原生提供 chunks；basic 模式下我们用 raw_content 兜底切片
    chunks = r.get("chunks")
    if not chunks and raw_content:
        # 每 ~1.5k 字符一段，最多 3 段，保证模型拿到的内容不至于太散
        chunks = [raw_content[i : i + 1500] for i in range(0, min(len(raw_content), 4500), 1500)]
    return {
        "title":   r.get("title", ""),
        "url":     r.get("url", ""),
        "snippet": r.get("content", ""),
        "chunks":  chunks or [],
    }


def _budget_scope() -> str:
    """
    本轮搜索预算的隔离 key。
    优先用 assistant_message_id（同一条助手回复期间共享），其次 conv_id，最后 "_default"。
    """
    try:
        from sandbox.context import current_message_id, current_conv_id
        msg_id = current_message_id.get()
        if msg_id:
            return f"msg:{msg_id}"
        conv_id = current_conv_id.get()
        if conv_id:
            return f"conv:{conv_id}"
    except Exception:
        pass
    return "_default"


async def _incr_count(scope: str) -> int | None:
    """递增计数；Redis 不可用时返回 None（降级为不限次）。"""
    try:
        from db.redis_state import incr_websearch_count
        return await incr_websearch_count(scope)
    except Exception as exc:
        logger.warning("web_search 预算计数失败（降级不限次）| scope=%s | %s", scope, exc)
        return None


async def _maybe_get_cached(scope: str, query: str) -> str | None:
    try:
        from db.redis_state import get_websearch_cache
        return await get_websearch_cache(scope, query)
    except Exception:
        return None


async def _maybe_set_cache(scope: str, query: str, value: str) -> None:
    try:
        from db.redis_state import set_websearch_cache
        await set_websearch_cache(scope, query, value)
    except Exception:
        pass


def _attach_note(payload: str, note: str) -> str:
    """给已生成的 JSON 结果挂一个 note 字段。"""
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            obj["note"] = note
            return json.dumps(obj, ensure_ascii=False)
    except Exception:
        pass
    return payload


def _budget_exhausted_payload(query: str, count: int) -> str:
    return json.dumps({
        "answer": "",
        "results": [],
        "note": (
            f"已达本轮搜索预算（{count - 1}/{_MAX_CALLS_PER_TURN} 次），"
            f"未执行「{query}」。请基于已搜集到的信息直接作答；"
            f"如确实信息不足，请坦诚告知用户而不是继续搜索。"
        ),
    }, ensure_ascii=False)


def _err_payload(title: str, snippet: str) -> str:
    """搜索失败的统一结构，与正常结果保持兼容。"""
    return json.dumps({
        "answer": "",
        "results": [{"title": title, "url": "", "snippet": snippet, "chunks": []}],
    }, ensure_ascii=False)
