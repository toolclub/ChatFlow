"""量化磁盘缓存（pickle.gz）

设计要点：
  - 零新依赖：pickle + gzip，pandas DataFrame 直接序列化
  - 原子写：写 .tmp 后 os.rename（POSIX 原子）
  - 文件按 (kind, key) 组织：
        ${ROOT}/spot/cn_a_2026-05-01.pkl.gz
        ${ROOT}/bars/cn_a_2026-04-30.pkl.gz   每天一份当日全市场 bar
        ${ROOT}/index/hs300.pkl.gz             每天覆盖
        ${ROOT}/meta/last_refresh.json
  - 滚窗清理：按文件名日期 + 总大小双约束
  - 所有 IO 在 asyncio.to_thread 里跑（pandas 同步 API）
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import os
import pickle
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from quant.execution import run_disk

from quant.config import (
    QUANT_BARS_LOOKBACK_DAYS,
    QUANT_CACHE_DIR,
    QUANT_CACHE_MAX_SIZE_MB,
    QUANT_CACHE_RETENTION_DAYS,
    QUANT_SPOT_FRESH_SECONDS,
)

logger = logging.getLogger("quant.cache_disk")


# ── 路径管理 ─────────────────────────────────────────────────────────────────

def _root() -> Path:
    p = Path(QUANT_CACHE_DIR).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    for sub in ("spot", "bars", "index", "meta"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def spot_path(market: str, day: date | None = None) -> Path:
    d = (day or date.today()).strftime("%Y-%m-%d")
    return _root() / "spot" / f"{market}_{d}.pkl.gz"


def bars_path(market: str, day: date) -> Path:
    d = day.strftime("%Y-%m-%d")
    return _root() / "bars" / f"{market}_{d}.pkl.gz"


def index_path(index_code: str) -> Path:
    return _root() / "index" / f"{index_code.lower()}.pkl.gz"


def meta_path() -> Path:
    return _root() / "meta" / "last_refresh.json"


_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.pkl\.gz$")


def _parse_file_date(p: Path) -> date | None:
    m = _DATE_RE.search(p.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


# ── 同步 IO（被 量化专用有界线程池包装） ─────────────────────────────────────

def _sync_write_df(path: Path, df: pd.DataFrame) -> int:
    """原子写：先写 .tmp，再 rename。返回字节数。"""
    if df is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=5) as f:
        pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
    size = tmp.stat().st_size
    os.replace(tmp, path)
    return size


def _sync_read_df(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, pd.DataFrame):
            return obj
        logger.warning("缓存文件 %s 不是 DataFrame：%s", path, type(obj))
        return None
    except Exception as exc:
        logger.warning("读缓存失败 %s: %s（已忽略）", path, exc)
        return None


# ── 异步 API ────────────────────────────────────────────────────────────────

async def write_spot(market: str, df: pd.DataFrame, day: date | None = None) -> int:
    if df is None or df.empty:
        return 0
    path = spot_path(market, day)
    size = await run_disk(_sync_write_df, path, df)
    logger.info("spot 写盘 market=%s day=%s rows=%d size=%dKB",
                market, (day or date.today()), len(df), size // 1024)
    return size


async def read_spot(market: str, day: date | None = None) -> pd.DataFrame | None:
    return await run_disk(_sync_read_df, spot_path(market, day))


async def spot_age_seconds(market: str, day: date | None = None) -> float | None:
    p = spot_path(market, day)
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


async def is_spot_fresh(
    market: str,
    day: date | None = None,
    fresh_seconds: int | None = None,
) -> bool:
    age = await spot_age_seconds(market, day)
    if age is None:
        return False
    return age <= (fresh_seconds if fresh_seconds is not None else QUANT_SPOT_FRESH_SECONDS)


async def write_bars(market: str, df: pd.DataFrame) -> int:
    """按 date 列拆分写入每日文件（合并已有数据，按 symbol 去重）。

    防御：sub / existing 任一方 columns 是 MultiIndex（来自历史 yfinance 脏数据
    或新版 yfinance 单 ticker 行为）时，强制展平到 level 0；否则 pd.concat 会报
    `Can only union MultiIndex with MultiIndex or Index of tuples`。
    """
    if df is None or df.empty or "date" not in df.columns:
        return 0

    def _split_and_merge() -> int:
        total = 0
        # df 自身列若是 MultiIndex，先整体展平
        df_flat = _flatten_columns(df)
        days = pd.to_datetime(df_flat["date"]).dt.date.unique()
        for d in days:
            sub = df_flat[pd.to_datetime(df_flat["date"]).dt.date == d]
            if sub.empty:
                continue
            # 合并已有数据，按 (symbol, date) 去重，保留最新
            existing = _sync_read_df(bars_path(market, d))
            if existing is not None and not existing.empty:
                existing = _flatten_columns(existing)
                merged = pd.concat([existing, sub], ignore_index=True)
                if "symbol" in merged.columns and "date" in merged.columns:
                    merged = merged.drop_duplicates(subset=["symbol", "date"], keep="last")
                sub = merged
            total += _sync_write_df(bars_path(market, d), sub.reset_index(drop=True))
        return total

    size = await run_disk(_split_and_merge)
    logger.info("bars 写盘 market=%s days=%d rows=%d size=%dKB",
                market, df["date"].nunique(), len(df), size // 1024)
    return size


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """归一化 columns：MultiIndex 展平 + 去除重复列名（保留最后出现）。

    历史 yfinance 脏数据可能写入了 MultiIndex 或带重复列名的 df（如多 ticker 误展平后
    "open" 出现多次），读出后 `row["open"]` 会返回 Series 而非标量，触发各种 ambiguous
    错误。本函数确保任何 df 出入磁盘前后列结构都是干净的单层 + 唯一名。
    """
    if df is None or df.empty:
        return df
    out = df
    if isinstance(out.columns, pd.MultiIndex):
        out = out.copy()
        out.columns = [c[0] if isinstance(c, tuple) else c for c in out.columns]
    # 去重列名：保留最后一个（新数据覆盖旧数据）
    if out.columns.duplicated().any():
        if out is df:
            out = df.copy()
        out = out.loc[:, ~out.columns.duplicated(keep="last")]
    return out


async def read_bars_for_symbol(
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> pd.DataFrame | None:
    """读取单只标的在 [start, end] 区间内的所有缓存 bars（纯读盘，不判断覆盖率）。"""
    def _read() -> pd.DataFrame | None:
        t0 = time.perf_counter()
        bars_dir = _root() / "bars"
        frames = []
        scanned = 0
        for p in sorted(bars_dir.glob(f"{market}_*.pkl.gz")):
            d = _parse_file_date(p)
            if d is None or d < start or d > end:
                continue
            scanned += 1
            sub = _sync_read_df(p)
            if sub is not None and not sub.empty:
                # 防御：历史脏数据可能 columns 是 MultiIndex 或含重复名，归一化后再筛
                sub = _flatten_columns(sub)
                if "symbol" in sub.columns:
                    match = sub[sub["symbol"].astype(str) == symbol]
                    if not match.empty:
                        frames.append(match)
        elapsed = (time.perf_counter() - t0) * 1000
        if elapsed > 500:
            logger.info("单标扫描 symbol=%s scanned=%d found=%d elapsed=%.0fms",
                        symbol, scanned, len(frames), elapsed)
        else:
            logger.debug("单标扫描 symbol=%s scanned=%d found=%d elapsed=%.0fms",
                        symbol, scanned, len(frames), elapsed)
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
    return await run_disk(_read)


async def read_bars_range(
    market: str,
    start: date,
    end: date,
) -> tuple[pd.DataFrame | None, list[date]]:
    """读 [start, end] 区间内已落盘的所有 bars，并返回缺失的日期列表。"""
    def _scan() -> tuple[pd.DataFrame | None, list[date]]:
        bars_dir = _root() / "bars"
        avail: dict[date, Path] = {}
        for p in bars_dir.glob(f"{market}_*.pkl.gz"):
            d = _parse_file_date(p)
            if d and start <= d <= end:
                avail[d] = p

        if not avail:
            # 全部缺：返回所有交易日候选（粗略：包含周末，service 层再 narrow）
            missing = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            return None, missing

        frames = []
        for d in sorted(avail):
            sub = _sync_read_df(avail[d])
            if sub is not None and not sub.empty:
                # 防御：历史脏数据归一化（MultiIndex 展平 + 列名去重）
                frames.append(_flatten_columns(sub))

        # 缺失的日期（实际历史交易日由调用方自行判断）
        present = set(avail.keys())
        all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        missing = [d for d in all_days if d not in present]

        if not frames:
            return None, missing
        return pd.concat(frames, ignore_index=True), missing

    return await run_disk(_scan)


async def write_index(index_code: str, symbols: list[str]) -> int:
    if not symbols:
        return 0
    df = pd.DataFrame({"symbol": list(symbols)})
    size = await run_disk(_sync_write_df, index_path(index_code), df)
    logger.info("index 写盘 %s count=%d", index_code, len(symbols))
    return size


async def read_index(index_code: str, max_age_seconds: int = 86400) -> list[str] | None:
    """index 文件 24h 内有效，过期返回 None。"""
    p = index_path(index_code)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > max_age_seconds:
        return None
    df = await run_disk(_sync_read_df, p)
    if df is None or "symbol" not in df.columns:
        return None
    return df["symbol"].astype(str).tolist()


# ── 元数据 ──────────────────────────────────────────────────────────────────

async def read_meta() -> dict:
    def _read() -> dict:
        p = meta_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return await run_disk(_read)


async def update_meta(patch: dict) -> None:
    def _update() -> None:
        p = meta_path()
        try:
            cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            cur = {}
        cur.update(patch)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    await run_disk(_update)


# ── 滚窗清理 ────────────────────────────────────────────────────────────────

async def prune(retention_days: int | None = None, max_total_mb: int | None = None) -> dict:
    """删除超龄 bars + 空盘上限保护。spot 只保留近 7 天。返回统计信息。"""
    rd = retention_days if retention_days is not None else QUANT_CACHE_RETENTION_DAYS
    mm = max_total_mb if max_total_mb is not None else QUANT_CACHE_MAX_SIZE_MB

    def _prune() -> dict:
        root = _root()
        deleted = []

        # bars 按日期保留
        cutoff = date.today() - timedelta(days=int(rd * 1.5))
        for p in (root / "bars").glob("*.pkl.gz"):
            d = _parse_file_date(p)
            if d is None or d < cutoff:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
                    deleted.append(p.name)

        # spot 只留 7 天
        spot_cutoff = date.today() - timedelta(days=7)
        for p in (root / "spot").glob("*.pkl.gz"):
            d = _parse_file_date(p)
            if d and d < spot_cutoff:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
                    deleted.append(p.name)

        # 总大小硬上限（FIFO 删最旧 bars）
        def _total_mb() -> float:
            return sum(p.stat().st_size for p in root.rglob("*.pkl.gz")) / 1024 / 1024

        total = _total_mb()
        if total > mm:
            files = sorted(
                (root / "bars").glob("*.pkl.gz"),
                key=lambda p: p.stat().st_mtime,
            )
            for f in files:
                if _total_mb() <= mm:
                    break
                with contextlib.suppress(FileNotFoundError):
                    f.unlink()
                    deleted.append(f.name)

        return {
            "deleted": deleted,
            "total_mb": round(_total_mb(), 2),
            "retention_days": rd,
            "max_total_mb": mm,
        }

    stats = await run_disk(_prune)
    logger.info("缓存清理 deleted=%d total=%.1fMB", len(stats["deleted"]), stats["total_mb"])
    return stats


# ── 状态/统计 ───────────────────────────────────────────────────────────────

async def cache_status() -> dict:
    """供前端 /cache/status 端点用，返回当前缓存的全景。"""
    def _status() -> dict:
        root = _root()

        spot_files = sorted(
            (root / "spot").glob("*.pkl.gz"),
            key=lambda p: _parse_file_date(p) or date.min,
            reverse=True,
        )
        bars_files = sorted(
            (root / "bars").glob("*.pkl.gz"),
            key=lambda p: _parse_file_date(p) or date.min,
            reverse=True,
        )
        index_files = list((root / "index").glob("*.pkl.gz"))

        def _info(path: Path | None):
            if not path or not path.exists():
                return None
            return {
                "name": path.name,
                "date": (_parse_file_date(path) or "").__str__() if _parse_file_date(path) else None,
                "age_seconds": int(time.time() - path.stat().st_mtime),
                "size_kb": int(path.stat().st_size / 1024),
            }

        total_kb = sum(p.stat().st_size for p in root.rglob("*.pkl.gz")) // 1024
        return {
            "root": str(root),
            "spot_latest": _info(spot_files[0] if spot_files else None),
            "spot_files": len(spot_files),
            "bars_latest": _info(bars_files[0] if bars_files else None),
            "bars_files": len(bars_files),
            "bars_oldest_date": str(_parse_file_date(bars_files[-1])) if bars_files else None,
            "index_files": [p.stem for p in index_files],
            "total_mb": round(total_kb / 1024, 2),
        }

    base = await run_disk(_status)
    base["meta"] = await read_meta()
    return base


# ── 调用方语义糖：默认 lookback 区间 ─────────────────────────────────────────

def default_bars_range(end: date | None = None) -> tuple[date, date]:
    e = end or date.today()
    s = e - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))
    return s, e
