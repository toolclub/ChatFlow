"""A 股公告 / 定增 / 减持 / 负面事件数据源（akshare + 东财），外加个股新闻与研报。

这是「股票预测 v2」设计文档 §1.1 / §2.3 的数据层落地。上游有两类消费者：
  1. 预测图（编排层直调，见文档 §4.1「不上 ToolNode」）：
     graph/prediction/announcements.py 拿走 fetch_* 的结果做三层判定
  2. 聊天工具（LLM function-call）：tools/builtin/finance/stock_news.py

★★★ 头号坑（文档 §1.1 末尾，已在容器里稳定复现 3 次以上）★★★
    akshare 的公告接口 **查到 0 条时不返回空表，而是抛 KeyError**：
        ak.stock_individual_notice_report(security='600519', begin_date='20260710', ...)
            → KeyError('代码')            # 源码 stock_notice.py:118 未判空直接 assign 列名
        ak.stock_ggcg_em(symbol='减持')
            → KeyError('减持')            # symbol_map 里根本没这个键（正确值是 '股东减持'）
    一只**没有任何负面公告的干净好股票**会因此把整条预测流水线炸掉 —— 这是最反直觉的。
    所以本模块所有 akshare 调用点一律走 `_ak_frame()`，KeyError → 空表 + logger.warning。

其余设计要点：
  - 铁律 #5：akshare 全是同步阻塞调用（内部 requests + tqdm 分页），一律 asyncio.to_thread
  - 铁律 #9：KeyError / 网络失败全部 logger.warning，绝不 `except: pass`
  - 网络类异常重试退避（1s/2s），仍失败 → 抛 AnnouncementDataError（让维度级降级去标 failed，
    而不是伪装成「这只票没有公告」—— 那是撒谎）
  - 全市场表（qbzf/ggcg/yjyg 都无入参，一拉就是全 A 股）走进程内 TTL 缓存 + 单飞锁，
    26 个维度并发时不会把东财打 26 遍
  - 每条返回都带 `confidence`（hard=结构化硬字段 / inferred=公告类型+标题推断）与 `source`，
    UI 必须原样透出（用户拍板：数据口径缺口可以接受，但不许假装精确）

口径缺口（诚实声明，UI 要写）：
  - 大股东减持：无结构化源（tushare stk_holdertrade 当前 token 无权限）。stock_ggcg_em 覆盖
    「股东增减持」但以董监高/持股 5% 以上股东的法定披露为主，未披露的场内交易一概看不到。
  - 违规/处罚：akshare 无专用数据集（穷举关键词零命中），stock_cg_lawsuit_cninfo 已损坏。
    只能用公司**自己披露**的风险类公告 + 业绩预告首亏/续亏，**不是监管处罚数据库**。
  - 只用标题 + 公告类型，**不解析 PDF 正文**（文档 §2.4）。
"""
from __future__ import annotations

import asyncio
import importlib
import hashlib
import logging
import re
import time
from datetime import date, timedelta

import pandas as pd

from quant.execution import run_io

logger = logging.getLogger("quant.data.announcement")


# ── 可调参数 ──────────────────────────────────────────────────────────────────

DEFAULT_MONTHS: int = 24            # 默认回溯窗口（文档 §2.3 第 2 层；PREDICTION_ANNOUNCE_MONTHS）

# 解禁是**前瞻**窗口（未来多少天内的解禁才算抛压），与上面的回溯窗口不是一回事。
# 维度描述写的是「未来 3 个月的解禁规模」→ 90 天。
LOCKUP_LOOKAHEAD_DAYS: int = 90
FULL_HISTORY_BEGIN: str = "19900101"

AK_RETRY_TIMES: int = 2             # 初次 + 2 次重试（退避 1s / 2s）
AK_BACKOFF_BASE: float = 1.0
AK_CALL_TIMEOUT: float = 60.0       # 单次 akshare 调用硬超时（全历史公告实测 6.5s / 22 页）

MARKET_TABLE_TTL: float = 1800.0    # 全市场表（qbzf/ggcg/yjyg）进程内缓存 30 分钟
YJYG_MAX_PERIODS: int = 6           # 业绩预告最多回看 6 个报告期（1.5 年），每期 = 1 次全市场拉取


class AnnouncementDataError(Exception):
    """公告数据源不可用（网络 / 限流 / 接口结构变更），重试耗尽后抛出。

    与「查到 0 条」严格区分：0 条走空列表（KeyError 兜底），不抛这个。
    调用方（预测图的数据型维度）应捕获它并把该维度标 failed + 展示原因，
    而不是当成「这只票很干净」。
    """

    def __init__(self, source: str, message: str) -> None:
        super().__init__(message)
        self.source = source
        self.message = message


# ── 公告类型规则表（文档 §2.3 第 1 层：0 次 LLM，纯 pandas filter）───────────────
#
# 判定**只看返回结果里的 `公告类型` 列**，不看入参 symbol —— 实测入参那套顶层分类
# 命中率极低（300750 用 symbol='持股变动' 只有 2 条无关记录），而且传非法值直接 KeyError。

# 硬噪音标题模板：**每家公司每年都要发**的例行文书，标题里天然带着风险词但毫无信息量。
# 必须先于一切规则判定（包括标题关键词网），否则会年年把它送进 LLM 白烧 token。
# 实测：600519（贵州茅台，干净票）24 个月内唯一的两条"负面候选"就是这个模板。
NOISE_TITLE_PATTERNS: tuple[str, ...] = (
    "非经营性资金占用及其他关联资金往来",   # 年报季标配，内容通常是"不存在资金占用"
    "募集资金存放与实际使用情况",
    "募集资金年度存放与使用情况",
    "内部控制评价报告",
    "内部控制审计报告",
    "独立董事年度述职报告",
    "独立董事关于",                        # 独董对各类事项的例行意见
    "监事会关于",
    "会计师事务所从事证券服务业务信息",
)

# 噪音白名单：常规治理类公告，实测占 60-70%，直接丢弃，不进 LLM
NOISE_TYPE_KEYWORDS: tuple[str, ...] = (
    "董事会决议", "监事会决议", "股东大会", "股东会", "调研活动", "分配方案",
    "审计报告", "法律意见书", "独立董事", "独董", "述职报告", "管理办法",
    "议事规则", "工作细则", "章程", "内部控制", "保荐/核查意见", "专项说明",
    "持续督导", "会计师事务所", "募集资金存放", "定期报告摘要", "年度报告摘要",
    "投资者关系", "高管人员任职变动", "证券事务代表", "更正公告", "澄清公告",
)

# 退市风险 —— 规则可直接定死严重度，无需 LLM
DELISTING_TYPE_KEYWORDS: tuple[str, ...] = (
    "实施退市风险警示", "退市风险警示", "退市整理期", "终止上市",
    "撤销风险警示", "撤销退市风险", "其他风险警示",
    # 「重大违法强制退市」语义无歧义 —— 规则直接定死 -1.0，省一次 LLM 往返。
    # 实测 *ST元道「关于公司股票可能被实施重大违法强制退市的第十次风险提示公告」原本要走 LLM。
    "强制退市",
)

VIOLATION_TYPE_KEYWORDS: tuple[str, ...] = ("违法违规",)

# 模糊风险类 → 进候选池（第 2 层），交给 LLM 精判（第 3 层）
RISK_TYPE_KEYWORDS: tuple[str, ...] = (
    "风险提示", "股份质押", "冻结", "交易异常波动", "重大事项", "诉讼", "仲裁",
    "业绩预告", "业绩快报", "商誉", "减值", "担保",
)

# 标题关键词 —— 公告类型没命中时的第二道网（文档 §1.1「替代」栏）
NEGATIVE_TITLE_KEYWORDS: tuple[str, ...] = (
    "问询函", "关注函", "监管函", "警示函", "监管措施", "行政处罚", "立案调查",
    "立案", "处罚", "违规", "违法", "涉嫌", "被起诉", "诉讼", "仲裁", "查封",
    "冻结", "质押", "失信", "逾期", "债务违约", "商誉减值", "计提减值",
    "退市", "风险警示", "异常波动", "终止", "撤回", "更正", "非标准审计意见",
    "无法表示意见", "保留意见", "股票停牌", "停牌", "资金占用",
)

REDUCTION_TYPE_KEYWORDS: tuple[str, ...] = ("减持",)
REDUCTION_TITLE_KEYWORDS: tuple[str, ...] = ("减持", "拟减持", "询价转让")

PLACEMENT_TYPE_KEYWORDS: tuple[str, ...] = ("增发", "配股", "定向增发")
# ★ 刻意**不收**「募集资金」：它指的是**已募资金的使用/管理**，不是新的增发。
#   实测 002594 收了它之后 93 条命中里 92 条是垃圾（「募集资金管理制度」「关于优质企业债券
#   募集资金使用情况之法律意见书」），且每条都会 needs_llm=True 送进 LLM —— 纯烧钱。
PLACEMENT_TITLE_KEYWORDS: tuple[str, ...] = (
    "非公开发行", "向特定对象发行", "定向增发", "增发", "配股",
)

# 业绩预告 `预告类型` → 负面度（东财硬结构化字段，识别爆雷最硬的信号）
# 枚举实测（20251231 全市场）：预增/减亏/首亏/增亏/预减/扭亏/略增/略减/续亏/续盈/不确定
YJYG_NEGATIVE_SEVERITY: dict[str, float] = {
    "首亏": -0.8,
    "增亏": -0.7,
    "续亏": -0.6,
    "预减": -0.4,
    "略减": -0.2,
    "不确定": -0.2,
}

# 对外的 event_type 契约（与 db 的 announcement_cache.event_type 列同构）
EVENT_NEGATIVE = "negative"
EVENT_PLACEMENT = "private_placement"
EVENT_REDUCTION = "stake_reduction"
EVENT_LOCKUP = "lockup"        # 限售解禁 —— 未来抛压（利空）
EVENT_BUYBACK = "buyback"      # 股份回购 —— 托底（抵消解禁抛压，不独立推高预测）
EVENT_NEUTRAL = "neutral"

CONF_HARD = "hard"          # 来自专用结构化数据集的硬字段（有金额/比例/发行价）
CONF_INFERRED = "inferred"  # 由公告类型 / 标题推断（可能再经 LLM 精判）


# ── 工具函数 ──────────────────────────────────────────────────────────────────

_EM_TAG = re.compile(r"</?em[^>]*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def strip_highlight(title: str) -> str:
    """剥离巨潮/东财搜索接口给标题加的 <em> 高亮标签（文档 §8.2：title_hash 前必须先剥）。

    不剥的后果：同一条公告在「搜索结果」与「列表结果」里 hash 不同 → 缓存永远打不中 →
    每次预测都重新烧 LLM。
    """
    if not title:
        return ""
    text = _EM_TAG.sub("", str(title))
    text = _HTML_TAG.sub("", text)
    return " ".join(text.split()).strip()


def title_hash(title: str) -> str:
    """md5(剥离高亮后的标题)，与 announcement_cache 的唯一键 (market,symbol,ann_date,title_hash) 对齐。"""
    return hashlib.md5(strip_highlight(title).encode("utf-8")).hexdigest()


def _plain_code(symbol: str) -> str:
    """000001.SZ / sz000001 / 000001 → 000001（akshare A 股接口一律吃 6 位纯数字）。"""
    sym = str(symbol or "").strip().upper()
    if "." in sym:
        sym = sym.split(".")[0]
    sym = re.sub(r"^(SH|SZ|BJ)", "", sym)
    return sym


def _hit(text: str, keywords: tuple[str, ...]) -> str | None:
    """返回第一个命中的关键词（用于 reason 里说清「凭什么这么判」）。"""
    if not text:
        return None
    for kw in keywords:
        if kw in text:
            return kw
    return None


def _iso_date(value: object) -> str:
    """东财的日期列可能是 str / Timestamp / NaT，统一成 'YYYY-MM-DD'；不可解析 → ''。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception as exc:
        logger.warning("日期解析失败 value=%r: %s", value, exc)
        return ""
    if ts is None or pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d")


def _num(value: object) -> float | None:
    """东财的数值列常有 '-' / '' / None；转不动就返回 None（不要伪造 0）。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f


def _cutoff(months: int) -> str:
    """回溯窗口起点（ISO 字符串）。months<=0 表示不限（全历史）。"""
    if months <= 0:
        return ""
    return (date.today() - timedelta(days=int(months) * 31)).isoformat()


def _window(months: int) -> tuple[str, str]:
    """akshare 的 begin_date/end_date 要 'YYYYMMDD'。"""
    end = date.today()
    if months <= 0:
        return FULL_HISTORY_BEGIN, end.strftime("%Y%m%d")
    begin = end - timedelta(days=int(months) * 31)
    return begin.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# ── akshare 调用护栏（KeyError → 空表；网络失败 → 重试 → AnnouncementDataError）──

async def _ak_frame(
    func_name: str,
    /,
    *args,
    source: str = "akshare",
    retries: int = AK_RETRY_TIMES,
    timeout: float = AK_CALL_TIMEOUT,
    **kwargs,
) -> pd.DataFrame:
    """调用 ak.<func_name>(*args, **kwargs)，返回 DataFrame。

    三种结局，语义严格区分（这正是本模块存在的理由）：
      1. 正常     → 返回 DataFrame
      2. KeyError → **查到 0 条**（akshare 未判空直接 assign 列名的老 bug）→ 返回空 DataFrame。
                    logger.warning 留痕，绝不静默（铁律 #9）。
      3. 其余异常 → 网络/限流/接口变更 → 退避重试；耗尽 → 抛 AnnouncementDataError。
                    **绝不降级成空表** —— 把「数据源挂了」说成「这只票很干净」是最危险的谎。
    """
    try:
        ak = await run_io(importlib.import_module, "akshare")
    except ImportError as exc:
        logger.warning("akshare 不可用: %s", exc)
        raise AnnouncementDataError(source, f"akshare 未安装：{exc}") from exc

    func = getattr(ak, func_name, None)
    if func is None:
        # akshare 版本间函数名会变，这里显式报出来，别让上游对着空表猜
        logger.warning("akshare 无 %s 函数（版本 %s）", func_name, getattr(ak, "__version__", "?"))
        raise AnnouncementDataError(source, f"akshare 缺少接口 {func_name}（版本不匹配）")

    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        if attempt:
            delay = AK_BACKOFF_BASE * (2 ** (attempt - 1))       # 1s / 2s
            logger.warning(
                "%s 第 %d/%d 次重试（%.0fs 后）: %s", func_name, attempt, retries, delay, last_exc,
            )
            await asyncio.sleep(delay)
        try:
            df = await asyncio.wait_for(
                run_io(func, *args, **kwargs), timeout=timeout,
            )
        except KeyError as exc:
            # ★ 头号坑：0 条结果 / 非法枚举入参都走这里。不是错误，是「没有数据」。
            logger.warning(
                "%s 抛 KeyError(%s) → 按「0 条结果」处理（akshare 空结果不返回空表的已知 bug）"
                " args=%r kwargs=%r",
                func_name, exc, args, kwargs,
            )
            return pd.DataFrame()
        except Exception as exc:      # noqa: BLE001 — 分类在下面做，这里先兜住再重试
            last_exc = exc
            logger.warning("%s 调用失败 args=%r kwargs=%r: %r", func_name, args, kwargs, exc)
            continue

        if df is None:
            logger.warning("%s 返回 None → 按空表处理", func_name)
            return pd.DataFrame()
        return df

    raise AnnouncementDataError(
        source,
        f"{func_name} 重试 {retries} 次仍失败：{type(last_exc).__name__}: {last_exc}",
    )


# ── 全市场表缓存（qbzf / ggcg / yjyg 都无 symbol 入参，一拉就是全 A 股）─────────

_TABLE_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_TABLE_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _TABLE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _TABLE_LOCKS[key] = lock
    return lock


async def _cached_table(
    key: str, func_name: str, *args, source: str = "akshare", **kwargs,
) -> pd.DataFrame:
    """带 TTL + 单飞锁的全市场表拉取。

    26 个维度可能并发跑，没有这层的话「定增」「减持」「爆雷」会把 stock_qbzf_em 各拉一遍。
    """
    now = time.time()
    hit = _TABLE_CACHE.get(key)
    if hit and now - hit[0] < MARKET_TABLE_TTL:
        return hit[1]

    async with _lock_for(key):
        hit = _TABLE_CACHE.get(key)          # 等锁期间别人可能已经拉好了
        if hit and time.time() - hit[0] < MARKET_TABLE_TTL:
            return hit[1]
        t0 = time.time()
        df = await _ak_frame(func_name, *args, source=source, **kwargs)
        _TABLE_CACHE[key] = (time.time(), df)
        logger.info(
            "全市场表 %s 刷新 rows=%d elapsed=%.1fs", key, len(df), time.time() - t0,
        )
        return df


def clear_table_cache() -> None:
    """清空进程内全市场表缓存（测试用）。"""
    _TABLE_CACHE.clear()


# ── 东财 datacenter 直连（绕开 akshare 的「全市场分页」灾难）────────────────────
#
# 为什么不用 ak.stock_ggcg_em：它内部请求的就是下面这个 API，但**不带 SECURITY_CODE 过滤**，
# 于是把全市场 224 页 / 111980 行减持记录**整个拉一遍** —— 实测单次调用 185s，
# 直接把预测流水线拖死（还会撞上 60s 硬超时 → 静默降级成「这只票没有减持」，更糟）。
# 同一个 API 加一条 (SECURITY_CODE="002594") 过滤 → **1 页 / 0.28s**。
#
# 这不是发明新数据源：URL / reportName / 字段全部照抄 akshare 的 stock_gdzjc_em.py，
# 只是把过滤条件下推到服务端。仓库里已有先例（quant/providers/akshare_provider.py 顶部注释：
# 东财接口用 requests 被 RST，改用 httpx 直连）。

EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_TIMEOUT: float = 20.0
EM_PAGE_SIZE: int = 500
EM_MAX_PAGES: int = 10          # 单只票不可能有 5000 条减持记录；防御性上限

# 东财原始 JSON 键 → 语义（键序与 akshare 的中文列名一一对应，已在容器里 zip 校验过）
#   CHANGE_NUM          持股变动-变动数量（万股）
#   AFTER_CHANGE_RATE   持股变动-占总股本比例（%）← 文档 §5.3 ③ 的 x_k 输入
#   CHANGE_FREE_RATIO   持股变动-占流通股比例（%）
#   AFTER_HOLDER_NUM    变动后持股总数（万股）
#   HOLD_RATIO          变动后占总股本比例（%）


async def _em_report(
    report_name: str, filters: str, *, source: str, sort: str = "END_DATE",
) -> list[dict]:
    """东财 datacenter 报表查询（服务端过滤 + 分页）。空结果 → []（不抛）。"""
    try:
        import httpx
    except ImportError as exc:
        logger.warning("httpx 不可用，无法直连东财 datacenter: %s", exc)
        raise AnnouncementDataError(source, f"httpx 未安装：{exc}") from exc

    rows: list[dict] = []
    last_exc: BaseException | None = None

    for attempt in range(AK_RETRY_TIMES + 1):
        if attempt:
            delay = AK_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning(
                "东财 %s 第 %d/%d 次重试（%.0fs 后）: %s",
                report_name, attempt, AK_RETRY_TIMES, delay, last_exc,
            )
            await asyncio.sleep(delay)

        rows = []
        try:
            async with httpx.AsyncClient(timeout=EM_TIMEOUT) as client:
                for page in range(1, EM_MAX_PAGES + 1):
                    resp = await client.get(EM_DATACENTER_URL, params={
                        "sortColumns": sort, "sortTypes": "-1",
                        "pageSize": str(EM_PAGE_SIZE), "pageNumber": str(page),
                        "reportName": report_name, "columns": "ALL",
                        "source": "WEB", "client": "WEB", "filter": filters,
                    })
                    resp.raise_for_status()
                    payload = resp.json()
                    result = payload.get("result")
                    if not result:
                        # 东财对「零命中」返回 result=null（不是空数组）—— 正常，不是故障
                        logger.info("东财 %s 零命中 filter=%s", report_name, filters)
                        return rows
                    data = result.get("data") or []
                    rows.extend(data)
                    if page >= int(result.get("pages") or 1):
                        break
            return rows
        except Exception as exc:      # noqa: BLE001 — 网络/HTTP，退避重试
            last_exc = exc
            logger.warning("东财 %s 请求失败 filter=%s: %r", report_name, filters, exc)

    raise AnnouncementDataError(
        source,
        f"东财 {report_name} 重试 {AK_RETRY_TIMES} 次仍失败："
        f"{type(last_exc).__name__}: {last_exc}",
    )


# ── 公告分类（纯函数，无 IO —— graph/prediction/announcements.py 可直接复用）────

def classify_notice(ann_type: str, title: str = "") -> dict:
    """按文档 §2.3 第 1/2 层对一条公告做规则判定。

    返回：
      {
        "event_type":    negative | private_placement | stake_reduction | neutral,
        "event_subtype": st_delisting_risk | violation | risk_warning | reduction |
                         placement | noise | other,
        "severity":      float | None,   # -1..0；None = 规则定不了，要第 3 层 LLM
        "needs_llm":     bool,           # True → 进候选池
        "is_noise":      bool,           # True → 直接丢弃，不进 LLM（省 token 的关键）
        "confidence":    "inferred",     # 公告口径永远是 inferred（只看标题+类型，不解析 PDF）
        "reason":        str,            # 凭什么这么判（命中的关键词）
      }
    """
    a_type = strip_highlight(ann_type or "")
    a_title = strip_highlight(title or "")
    both = f"{a_type} {a_title}"

    def _res(event_type, subtype, severity, needs_llm, is_noise, reason):
        return {
            "event_type": event_type,
            "event_subtype": subtype,
            "severity": severity,
            "needs_llm": needs_llm,
            "is_noise": is_noise,
            "confidence": CONF_INFERRED,
            "reason": reason,
        }

    # ⓪ 硬噪音模板 —— 优先于一切（含标题关键词网）。这些例行文书标题自带「资金占用」
    #    「募集资金」等风险词，不先拦下来，每家公司每年都会白白进一次 LLM。
    kw = _hit(a_title, NOISE_TITLE_PATTERNS)
    if kw:
        return _res(EVENT_NEUTRAL, "noise", 0.0, False, True, f"例行文书模板（{kw}），不进 LLM")

    # ① 退市风险 —— 规则可直接定死，无需 LLM
    kw = _hit(a_type, DELISTING_TYPE_KEYWORDS) or _hit(a_title, DELISTING_TYPE_KEYWORDS)
    if kw:
        # 「撤销风险警示」是利好（摘帽），别把它当成 -1.0 的爆雷
        if "撤销" in both:
            return _res(EVENT_NEUTRAL, "st_revoke", 0.0, False, False, f"公告类型命中「{kw}」（撤销风险警示，非负面）")
        return _res(EVENT_NEGATIVE, "st_delisting_risk", -1.0, False, False, f"公告类型命中「{kw}」（退市风险，规则定级）")

    # ② 减持（含增持要排除）
    kw = _hit(a_type, REDUCTION_TYPE_KEYWORDS) or _hit(a_title, REDUCTION_TITLE_KEYWORDS)
    if kw and "增持" not in both:
        return _res(EVENT_REDUCTION, "reduction", None, True, False, f"命中「{kw}」（减持类公告，需 LLM 判进展/预披露/完成）")

    # ③ 增发 / 配股
    kw = _hit(a_type, PLACEMENT_TYPE_KEYWORDS) or _hit(a_title, PLACEMENT_TITLE_KEYWORDS)
    if kw:
        return _res(EVENT_PLACEMENT, "placement", None, True, False, f"命中「{kw}」（增发/配股类公告）")

    # ④ 违法违规 —— 命中即负面，但严重度交 LLM 分级（一纸问询函 ≠ 被立案调查）
    kw = _hit(a_type, VIOLATION_TYPE_KEYWORDS)
    if kw:
        return _res(EVENT_NEGATIVE, "violation", None, True, False, f"公告类型命中「{kw}」（严重度待 LLM 分级）")

    # ⑤ 噪音白名单 —— 放在风险类之后：「董事会决议」是噪音，但「关于收到问询函的公告」不是
    kw = _hit(a_type, NOISE_TYPE_KEYWORDS)
    if kw and not _hit(a_title, NEGATIVE_TITLE_KEYWORDS):
        return _res(EVENT_NEUTRAL, "noise", 0.0, False, True, f"常规治理类公告（{kw}），不进 LLM")

    # ⑥ 模糊风险类 / 标题关键词 → 候选池
    kw = _hit(a_type, RISK_TYPE_KEYWORDS) or _hit(a_title, NEGATIVE_TITLE_KEYWORDS)
    if kw:
        return _res(EVENT_NEGATIVE, "risk_warning", None, True, False, f"命中风险关键词「{kw}」（候选池，待 LLM 精判）")

    return _res(EVENT_NEUTRAL, "other", 0.0, False, True, "未命中任何风险/资本运作规则")


# ── 公开 API ①：全量历史公告 ──────────────────────────────────────────────────

async def fetch_stock_notices(
    symbol: str,
    *,
    market: str = "cn_a",
    months: int = 0,
    limit: int | None = None,
    classify: bool = False,
) -> list[dict]:
    """个股公告列表（东财 `stock_individual_notice_report`），按公告日期倒序。

    Args:
        symbol:   000001 / 000001.SZ 均可
        market:   cn_a（美股请走 quant.data.sec_edgar.fetch_submissions）
        months:   回溯月数；**0 = 全历史**（实测 000001 → 1733 条 / 6.5s，22 页）
        limit:    只要最近 N 条（聊天工具用；预测维度不传，要全量）
        classify: True 时给每条附 classify_notice() 的规则判定字段

    Returns（每条）:
        {"symbol","market","name","title","ann_type","ann_date","url",
         "source":"eastmoney_notice","title_hash","confidence":"inferred"
         [, "event_type","event_subtype","severity","needs_llm","is_noise","reason"]}

    0 条 → 返回 []（akshare 会抛 KeyError('代码')，已在 _ak_frame 里兜住）。
    数据源挂了 → 抛 AnnouncementDataError（**不会**伪装成 []）。
    """
    if market != "cn_a":
        logger.warning("fetch_stock_notices 只支持 A 股，market=%s → 返回 []", market)
        return []

    code = _plain_code(symbol)
    if not code:
        logger.warning("fetch_stock_notices 收到空/非法代码: %r", symbol)
        return []

    begin, end = _window(months)
    df = await _ak_frame(
        "stock_individual_notice_report",
        source="eastmoney_notice",
        security=code, symbol="全部", begin_date=begin, end_date=end,
    )
    if df.empty:
        logger.info("fetch_stock_notices %s(%s~%s) → 0 条", code, begin, end)
        return []

    items: list[dict] = []
    for row in df.to_dict("records"):
        title = strip_highlight(row.get("公告标题", ""))
        if not title:
            continue
        item = {
            "symbol": code,
            "market": market,
            "name": str(row.get("名称", "") or ""),
            "title": title,
            "ann_type": strip_highlight(row.get("公告类型", "")),
            "ann_date": _iso_date(row.get("公告日期")),
            "url": str(row.get("网址", "") or ""),
            "source": "eastmoney_notice",
            "title_hash": title_hash(title),
            "confidence": CONF_INFERRED,
        }
        if classify:
            item.update(classify_notice(item["ann_type"], title))
        items.append(item)

    items.sort(key=lambda x: x["ann_date"], reverse=True)
    if limit and limit > 0:
        items = items[:limit]
    logger.info("fetch_stock_notices %s → %d 条 (%s~%s)", code, len(items), begin, end)
    return items


# ── 公开 API ②：负面事件 / 爆雷 ───────────────────────────────────────────────

async def fetch_negative_events(
    symbol: str,
    *,
    market: str = "cn_a",
    months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """负面/爆雷候选事件（文档 §2.3 第 1+2 层，**不含 LLM**）。

    A 股 = 风险类公告（规则命中 + 候选池）+ 业绩预告首亏/续亏/预减（硬结构化字段）。
    美股 = 委托 sec_edgar（8-K Item 4.02 / NT 10-K 等），见 quant.data.sec_edgar。

    第 3 层 LLM 精判由 graph/prediction/announcements.py 做：只需挑出 needs_llm=True 的条目。

    Returns（每条）:
        {"symbol","market","event_type":"negative","event_subtype","severity"(float|None),
         "needs_llm","confidence"("hard"|"inferred"),"reason","title","ann_type","ann_date",
         "url","source","title_hash", ...业绩预告额外带 "forecast_type"/"forecast_value"/"change_pct"}

    severity=None 表示「规则判定不了，等 LLM」；不要把它当 0。
    """
    if market == "us_stock":
        from quant.data import sec_edgar
        return await sec_edgar.fetch_negative_events(symbol, months=months)

    events: list[dict] = []

    # ── 来源 A：风险类公告（主源，失败即抛 —— 这是该维度的地基）
    notices = await fetch_stock_notices(symbol, market=market, months=months, classify=True)
    for n in notices:
        if n.get("is_noise") or n.get("event_type") != EVENT_NEGATIVE:
            continue
        events.append({
            **{k: v for k, v in n.items() if k not in ("is_noise",)},
            "confidence": CONF_INFERRED,
        })

    # ── 来源 B：业绩预告（副源，失败降级 —— 别让它拖垮已经拿到的公告结果）
    try:
        events.extend(await _fetch_earnings_warnings(symbol, market=market, months=months))
    except AnnouncementDataError as exc:
        logger.warning(
            "业绩预告拉取失败（降级：仅用公告判定爆雷）symbol=%s: %s", symbol, exc,
        )

    events.sort(key=lambda x: x.get("ann_date") or "", reverse=True)
    logger.info(
        "fetch_negative_events %s → %d 条（其中待 LLM 精判 %d 条）",
        _plain_code(symbol), len(events), sum(1 for e in events if e.get("needs_llm")),
    )
    return events


async def _fetch_earnings_warnings(
    symbol: str, *, market: str, months: int,
) -> list[dict]:
    """业绩预告首亏/续亏/预减（`stock_yjyg_em`）—— 识别爆雷最硬的结构化字段。

    接口按**报告期**取全市场（无 symbol 入参），所以按窗口生成报告期列表逐个拉 + 全市场缓存。
    """
    code = _plain_code(symbol)
    if not code:
        return []

    out: list[dict] = []
    for period in _report_periods(months)[:YJYG_MAX_PERIODS]:
        df = await _cached_table(
            f"yjyg:{period}", "stock_yjyg_em", source="eastmoney_yjyg", date=period,
        )
        if df.empty or "股票代码" not in df.columns:
            continue
        sub = df[df["股票代码"].astype(str) == code]
        if sub.empty:
            continue
        for row in sub.to_dict("records"):
            f_type = str(row.get("预告类型", "") or "").strip()
            severity = YJYG_NEGATIVE_SEVERITY.get(f_type)
            if severity is None:
                continue            # 预增/扭亏/减亏… 不是负面事件
            title = f"{row.get('股票简称', '')}: {period} 业绩预告「{f_type}」"
            out.append({
                "symbol": code,
                "market": market,
                "event_type": EVENT_NEGATIVE,
                "event_subtype": "earnings_warning",
                "severity": severity,
                "needs_llm": False,                 # 硬字段，规则直接定级
                "confidence": CONF_HARD,            # ★ 东财结构化枚举，不是标题推断
                "reason": f"业绩预告类型「{f_type}」（东财结构化字段，规则定级 {severity}）",
                "title": title,
                "ann_type": "业绩预告",
                "ann_date": _iso_date(row.get("公告日期")),
                "url": "",
                "source": "eastmoney_yjyg",
                "title_hash": title_hash(title),
                "report_period": period,
                "forecast_type": f_type,
                "forecast_metric": str(row.get("预测指标", "") or ""),
                "forecast_value": _num(row.get("预测数值")),
                "change_pct": _num(row.get("业绩变动幅度")),
                "change_reason": str(row.get("业绩变动原因", "") or "")[:300],
            })
    return out


def _report_periods(months: int) -> list[str]:
    """窗口内的报告期列表（YYYYMMDD，倒序）：0331 / 0630 / 0930 / 1231。"""
    today = date.today()
    span = max(int(months), 3) if months > 0 else 36
    earliest = today - timedelta(days=span * 31)
    periods: list[str] = []
    for year in range(today.year, earliest.year - 1, -1):
        for md in ("1231", "0930", "0630", "0331"):
            d = date(year, int(md[:2]), int(md[2:]))
            if d > today or d < earliest:
                continue
            periods.append(f"{year}{md}")
    return periods


# ── 公开 API ③：定增 / 增发 ───────────────────────────────────────────────────

async def fetch_placements(
    symbol: str,
    *,
    market: str = "cn_a",
    months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """定增/增发/配股。

    A 股双源：
      - **硬源** `stock_qbzf_em()`：全市场增发表，含发行方式/发行总数/**发行价格**/最新价/锁定期
        → confidence=hard，可直接算折价率（文档 §5.3 ② 的 x_j）
      - **软源** 公告类型含「增发/配股」→ confidence=inferred（只有标题，没有价格）
    美股：form ∈ S-3 / S-3ASR / 424B2/3/5（委托 sec_edgar）。

    Returns（每条）:
        {"symbol","market","event_type":"private_placement","source","confidence",
         "issue_method","shares","price","latest_price","discount_rate","lockup",
         "issue_date","listing_date","ann_date","title","ann_type","url","title_hash","reason"}

    discount_rate = (最新价 − 发行价)/最新价，取不到价格时为 None。
    **摊薄比例（发行股数/总股本）本模块给不了** —— 总股本不在这张表里，由 engine 兜底 0.05
    并在 UI 标「估算」（文档 §5.3 ②）。
    """
    if market == "us_stock":
        from quant.data import sec_edgar
        return await sec_edgar.fetch_placements(symbol, months=months)

    code = _plain_code(symbol)
    if not code:
        return []

    out: list[dict] = []
    cutoff = _cutoff(months)

    # ── 硬源：全市场增发表
    try:
        df = await _cached_table("qbzf", "stock_qbzf_em", source="eastmoney_qbzf")
    except AnnouncementDataError as exc:
        logger.warning("增发表 stock_qbzf_em 拉取失败（降级：仅用公告推断）: %s", exc)
        df = pd.DataFrame()

    if not df.empty and "股票代码" in df.columns:
        for row in df[df["股票代码"].astype(str) == code].to_dict("records"):
            issue_date = _iso_date(row.get("发行日期"))
            if cutoff and issue_date and issue_date < cutoff:
                continue
            price = _num(row.get("发行价格"))
            latest = _num(row.get("最新价"))
            discount = None
            if price is not None and latest not in (None, 0):
                discount = round((latest - price) / latest, 6)
            method = str(row.get("发行方式", "") or "")
            title = f"{row.get('股票简称', '')}: {method}（发行价 {price}，发行 {row.get('发行总数')} 股）"
            out.append({
                "symbol": code,
                "market": market,
                "event_type": EVENT_PLACEMENT,
                "event_subtype": "placement",
                "source": "eastmoney_qbzf",
                "confidence": CONF_HARD,          # ★ 有发行价/发行数，能算折价
                "issue_method": method,
                "shares": _num(row.get("发行总数")),
                "price": price,
                "latest_price": latest,
                "discount_rate": discount,
                "dilution_ratio": None,           # 总股本不在本表，engine 兜底
                "lockup": str(row.get("锁定期", "") or ""),
                "issue_date": issue_date,
                "listing_date": _iso_date(row.get("增发上市日期")),
                "ann_date": issue_date,
                "title": title,
                "ann_type": method or "增发",
                "url": "",
                "title_hash": title_hash(title),
                "severity": None,
                "needs_llm": False,
                "reason": "东财增发表结构化记录（发行方式/发行价/发行数均为硬字段）",
            })

    # ── 软源：增发类公告（能拿到「预案/获准/上市」这类进展阶段，硬源只有已发行的）
    notices = await fetch_stock_notices(symbol, market=market, months=months, classify=True)
    for n in notices:
        if n.get("event_type") != EVENT_PLACEMENT:
            continue
        out.append({
            "symbol": code,
            "market": market,
            "event_type": EVENT_PLACEMENT,
            "event_subtype": "placement",
            "source": "eastmoney_notice",
            "confidence": CONF_INFERRED,          # ★ 只有标题，没有发行价
            "issue_method": "",
            "shares": None, "price": None, "latest_price": None,
            "discount_rate": None, "dilution_ratio": None, "lockup": "",
            "issue_date": "", "listing_date": "",
            "ann_date": n["ann_date"],
            "title": n["title"],
            "ann_type": n["ann_type"],
            "url": n["url"],
            "title_hash": n["title_hash"],
            "severity": None,
            "needs_llm": True,                    # 交 LLM 判「预案 / 已发行 / 终止」
            "reason": n.get("reason", ""),
        })

    out.sort(key=lambda x: x.get("ann_date") or "", reverse=True)
    logger.info("fetch_placements %s → %d 条（hard=%d）", code, len(out),
                sum(1 for x in out if x["confidence"] == CONF_HARD))
    return out


# ── 公开 API ④：股东减持 ──────────────────────────────────────────────────────

async def fetch_reductions(
    symbol: str,
    *,
    market: str = "cn_a",
    months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """股东减持。

    A 股双源：
      - **硬源** `stock_ggcg_em(symbol='股东减持')`：东财「股东增减持」明细，含
        **变动数量 / 占总股本比例 / 占流通股比例 / 变动区间 / 股东名称** → confidence=hard，
        `pct_of_total` 直接就是文档 §5.3 ③ 的 x_k 输入。
        ★ 注意入参：akshare 1.18.64 的 symbol_map 只认 {"全部","股东增持","股东减持"}，
          设计文档里写的 `symbol='减持'` 会直接 KeyError('减持')（已实测）。
      - **软源** 公告类型/标题含「减持」→ confidence=inferred（大股东减持的唯一可得口径）

    美股：form 4（内部人交易）/ 144（拟售）—— **只算条数，不解析金额**（委托 sec_edgar）。

    Returns（每条）:
        {"symbol","market","event_type":"stake_reduction","source","confidence",
         "holder","direction","shares_wan","pct_of_total","pct_of_float",
         "after_shares_wan","after_pct_of_total","start_date","end_date",
         "ann_date","title","ann_type","url","title_hash","severity","needs_llm","reason"}

    口径缺口（UI 必须写）：未披露的场内交易看不到；大股东部分只有标题级识别。
    """
    if market == "us_stock":
        from quant.data import sec_edgar
        return await sec_edgar.fetch_reductions(symbol, months=months)

    code = _plain_code(symbol)
    if not code:
        return []

    out: list[dict] = []
    cutoff = _cutoff(months)

    # ── 硬源：东财股东增减持明细（服务端按代码过滤，1 页 / <0.5s）
    try:
        rows = await _em_report(
            "RPT_SHARE_HOLDER_INCREASE",
            f'(DIRECTION="减持")(SECURITY_CODE="{code}")',
            source="eastmoney_ggcg",
        )
    except AnnouncementDataError as exc:
        logger.warning("东财减持明细拉取失败（降级：仅用公告推断，无金额）: %s", exc)
        rows = []

    for row in rows:
        ann_date = _iso_date(row.get("NOTICE_DATE"))
        if cutoff and ann_date and ann_date < cutoff:
            continue
        holder = str(row.get("HOLDER_NAME", "") or "")
        pct = _num(row.get("AFTER_CHANGE_RATE"))        # 本次变动占总股本比例（%）
        shares_wan = _num(row.get("CHANGE_NUM"))        # 变动数量（万股）
        channel = str(row.get("MARKET", "") or "")      # 二级市场 / 大宗交易 …
        title = (
            f"{row.get('SECURITY_NAME_ABBR', '')}: {holder} 减持 {shares_wan} 万股"
            f"（占总股本 {pct}%{'，' + channel if channel else ''}）"
        )
        out.append({
            "symbol": code,
            "market": market,
            "event_type": EVENT_REDUCTION,
            "event_subtype": "reduction",
            "source": "eastmoney_ggcg",
            "confidence": CONF_HARD,          # ★ 有股数/比例，能算抛压强度
            "holder": holder,
            "direction": str(row.get("DIRECTION", "") or "减持"),
            "channel": channel,
            "shares_wan": shares_wan,
            "pct_of_total": pct,
            "pct_of_float": _num(row.get("CHANGE_FREE_RATIO")),
            "after_shares_wan": _num(row.get("AFTER_HOLDER_NUM")),
            "after_pct_of_total": _num(row.get("HOLD_RATIO")),
            "avg_price": _num(row.get("TRADE_AVERAGE_PRICE")),
            "start_date": _iso_date(row.get("START_DATE")),
            "end_date": _iso_date(row.get("END_DATE")),
            "ann_date": ann_date,
            "title": title,
            "ann_type": "股东减持",
            "url": "",
            "title_hash": title_hash(title),
            "severity": None,
            "needs_llm": False,
            "reason": "东财股东增减持明细（变动数量/占总股本比例为硬字段）",
        })

    # ── 软源：减持类公告（大股东/实际控制人减持的唯一可得口径）
    notices = await fetch_stock_notices(symbol, market=market, months=months, classify=True)
    for n in notices:
        if n.get("event_type") != EVENT_REDUCTION:
            continue
        out.append({
            "symbol": code,
            "market": market,
            "event_type": EVENT_REDUCTION,
            "event_subtype": "reduction",
            "source": "eastmoney_notice",
            "confidence": CONF_INFERRED,          # ★ 标题级识别，无金额
            "holder": "",
            "direction": "减持",
            "shares_wan": None, "pct_of_total": None, "pct_of_float": None,
            "after_shares_wan": None, "after_pct_of_total": None,
            "start_date": "", "end_date": "",
            "ann_date": n["ann_date"],
            "title": n["title"],
            "ann_type": n["ann_type"],
            "url": n["url"],
            "title_hash": n["title_hash"],
            "severity": None,
            "needs_llm": True,                    # 交 LLM 判「预披露 / 实施 / 完成 / 终止」
            "reason": n.get("reason", ""),
        })

    out.sort(key=lambda x: x.get("ann_date") or "", reverse=True)
    logger.info("fetch_reductions %s → %d 条（hard=%d）", code, len(out),
                sum(1 for x in out if x["confidence"] == CONF_HARD))
    return out


# ── 公开 API ④b：限售解禁（抛压）+ 股份回购（托底）─────────────────────────────

async def fetch_lockup_buyback(
    symbol: str,
    *,
    market: str = "cn_a",
    months: int = DEFAULT_MONTHS,
) -> list[dict]:
    """限售解禁（未来抛压）+ 股份回购（托底）。**A 股专有**。

    与其它 fetcher 的两点根本不同（下游打分要按这个来）：

    1. **解禁是前瞻的，不是回溯的。** 别的维度看"过去 N 个月发生了什么"，解禁看
       "未来 N 个月有多少股要砸出来"。所以这里筛的是 `解禁时间 >= 今天`，并额外给出
       `days_until`（还有几天解禁）。时间衰减也必须按"临近度"算 —— 越近的解禁压力越大，
       用回溯的 decay_weight(ann_date) 会把未来日期算出 >1 的权重（exp(+x)），必须避开。

    2. **回购是利多，但本模型的事件分被钳在 [-1, 0]**（engine.py:540），表达不了正分。
       这与维度语义是一致的：文档写的是「回购（**托底**）」—— 托底 = 缓冲抛压，而不是
       独立推高预测。所以回购项 severity=0（不贡献负分），只带 `support_ratio` 字段，
       由 announcements.compute_dimension_score 在净额里**抵消**解禁压力。
       ★ 这是一个建模简化，UI 的口径说明里要讲清楚。

    数据源（均实测可用）：
      - 解禁 `stock_restricted_release_queue_em(symbol=code)`：按标的直查，含
        解禁时间/解禁数量/实际解禁数量市值/**占总市值比例**/限售股类型。
        `占总市值比例` 就是抛压强度的直接输入 → confidence=hard。
      - 回购 `stock_repurchase_em()`：**全市场表**，需按 `股票代码` 客户端过滤
        （akshare 没提供按标的过滤的入参）。含计划回购金额区间 / 占总股本比例 / 起始时间。

    美股：akshare 无对应接口。dimensions.py 已把该维度限制为 cn_a，这里再兜一层，
    返回 [] 而不是抛错（防止有人绕过注册表直调）。

    Returns（每条）:
        {"symbol","market","event_type":"lockup"|"buyback","source","confidence",
         "ann_date","days_until","lockup_pct","shares","market_value","share_type",
         "support_ratio","amount_low","amount_high",
         "title","ann_type","url","title_hash","severity","needs_llm","reason"}
    """
    if market != "cn_a":
        logger.info("fetch_lockup_buyback 仅支持 A 股（market=%s）→ 返回空", market)
        return []

    code = _plain_code(symbol)
    if not code:
        return []

    out: list[dict] = []
    today = date.today()
    horizon = today + timedelta(days=int(LOCKUP_LOOKAHEAD_DAYS))

    # ── 解禁：未来窗口内的限售股解禁（抛压）────────────────────────────────
    df = await _ak_frame(
        "stock_restricted_release_queue_em",
        symbol=code,
        source="eastmoney_lockup",
    )
    for row in df.to_dict("records"):
        d = _iso_date(row.get("解禁时间"))
        if not d:
            continue
        try:
            d_obj = date.fromisoformat(d)
        except ValueError:
            continue
        # 只要未来的（已经解禁完的是历史，不构成未来抛压）
        if d_obj < today or d_obj > horizon:
            continue

        pct = _num(row.get("占总市值比例"))          # 百分数：1.2 表示 1.2%
        shares = _num(row.get("解禁数量"))
        mval = _num(row.get("实际解禁数量市值"))
        stype = str(row.get("限售股类型", "") or "")
        days_until = (d_obj - today).days

        title = (
            f"限售解禁 {d} · {stype or '限售股'}"
            f"（占总市值 {pct if pct is not None else '?'}%"
            f"，{days_until} 天后）"
        )
        out.append({
            "symbol": code,
            "market": market,
            "event_type": EVENT_LOCKUP,
            "source": "eastmoney_lockup",
            "confidence": CONF_HARD,     # 占总市值比例是硬字段，不是 LLM 猜的
            "ann_date": d,
            "days_until": days_until,
            "lockup_pct": pct,
            "shares": shares,
            "market_value": mval,
            "share_type": stype,
            "title": title,
            "ann_type": "限售解禁",
            "url": "",
            "title_hash": title_hash(title),
            "severity": None,            # 强度由 lockup_pct 算，不用 severity 通道
            "needs_llm": False,          # ★ 结构化硬数据 → 规则层定案，0 次 LLM
            "reason": (
                f"未来 {days_until} 天解禁，占总市值 {pct}%" if pct is not None
                else f"未来 {days_until} 天有限售股解禁（规模未披露）"
            ),
        })

    # ── 回购：进行中/已公告的回购计划（托底）────────────────────────────────
    try:
        rp = await _cached_table(
            "stock_repurchase_em", "stock_repurchase_em", source="eastmoney_buyback",
        )
    except AnnouncementDataError as exc:
        logger.warning("回购全市场表拉取失败（降级：只算解禁抛压）: %s", exc)
        rp = pd.DataFrame()

    if not rp.empty and "股票代码" in rp.columns:
        mine = rp[rp["股票代码"].astype(str).str.zfill(6) == code]
        cutoff = _cutoff(months)
        for row in mine.to_dict("records"):
            d = _iso_date(row.get("回购起始时间"))
            if cutoff and d and d < cutoff:
                continue
            lo = _num(row.get("占公告前一日总股本比例-下限"))
            hi = _num(row.get("占公告前一日总股本比例-上限"))
            support = hi if hi is not None else lo      # 百分数
            amt_lo = _num(row.get("计划回购金额区间-下限"))
            amt_hi = _num(row.get("计划回购金额区间-上限"))

            title = (
                f"股份回购计划 {d or ''}"
                f"（占总股本 {support if support is not None else '?'}%）"
            )
            out.append({
                "symbol": code,
                "market": market,
                "event_type": EVENT_BUYBACK,
                "source": "eastmoney_buyback",
                "confidence": CONF_HARD,
                "ann_date": d,
                "days_until": None,
                "support_ratio": support,
                "amount_low": amt_lo,
                "amount_high": amt_hi,
                "title": title,
                "ann_type": "股份回购",
                "url": "",
                "title_hash": title_hash(title),
                "severity": 0.0,         # ★ 回购不贡献负分（事件分钳在 [-1,0]，表达不了利多）
                "needs_llm": False,
                "reason": f"回购计划托底，占总股本 {support}%" if support is not None
                          else "已公告回购计划（规模未披露）",
            })

    out.sort(key=lambda x: x.get("ann_date") or "")
    n_lock = sum(1 for x in out if x["event_type"] == EVENT_LOCKUP)
    logger.info(
        "fetch_lockup_buyback %s → %d 条（解禁 %d / 回购 %d，前瞻 %d 天）",
        code, len(out), n_lock, len(out) - n_lock, LOCKUP_LOOKAHEAD_DAYS,
    )
    return out


# ── 公开 API ⑤/⑥：个股新闻 / 研报（复活 tools/builtin/finance/stock_news.py）────

async def fetch_stock_news(symbol: str, *, limit: int = 8) -> list[dict]:
    """个股新闻（东财 `stock_news_em`，固定返回最近 ~10 条）。

    Returns: [{"date","title","source","url","summary"}]
    """
    code = _plain_code(symbol)
    if not code:
        return []
    df = await _ak_frame("stock_news_em", source="eastmoney_news", symbol=code)
    if df.empty:
        return []

    items: list[dict] = []
    for row in df.to_dict("records"):
        title = strip_highlight(row.get("新闻标题", ""))
        if not title:
            continue
        items.append({
            "date": str(row.get("发布时间", "") or ""),
            "title": title,
            "source": str(row.get("文章来源", "") or ""),
            "url": str(row.get("新闻链接", "") or ""),
            "summary": strip_highlight(str(row.get("新闻内容", "") or ""))[:200],
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[: max(1, limit)]


async def fetch_research_reports(symbol: str, *, limit: int = 8) -> list[dict]:
    """个股研报（东财 `stock_research_report_em`）。

    Returns: [{"date","title","org","rating","url","eps_forecast"}]
    """
    code = _plain_code(symbol)
    if not code:
        return []
    df = await _ak_frame("stock_research_report_em", source="eastmoney_report", symbol=code)
    if df.empty:
        return []

    # 「2026-盈利预测-收益」这类列名带年份，动态找第一个
    eps_col = next((c for c in df.columns if "盈利预测-收益" in str(c)), None)

    items: list[dict] = []
    for row in df.to_dict("records"):
        title = strip_highlight(row.get("报告名称", ""))
        if not title:
            continue
        items.append({
            "date": _iso_date(row.get("日期")),
            "title": title,
            "org": str(row.get("机构", "") or ""),
            "rating": str(row.get("东财评级", "") or ""),
            "url": str(row.get("报告PDF链接", "") or ""),
            "eps_forecast": _num(row.get(eps_col)) if eps_col else None,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[: max(1, limit)]


__all__ = [
    "AnnouncementDataError",
    "fetch_stock_notices",
    "fetch_negative_events",
    "fetch_placements",
    "fetch_reductions",
    "fetch_lockup_buyback",
    "fetch_stock_news",
    "fetch_research_reports",
    "classify_notice",
    "strip_highlight",
    "title_hash",
    "clear_table_cache",
    "DEFAULT_MONTHS",
    "LOCKUP_LOOKAHEAD_DAYS",
    "EVENT_NEGATIVE",
    "EVENT_PLACEMENT",
    "EVENT_LOCKUP",
    "EVENT_BUYBACK",
    "EVENT_REDUCTION",
    "EVENT_NEUTRAL",
    "CONF_HARD",
    "CONF_INFERRED",
    "YJYG_NEGATIVE_SEVERITY",
]
