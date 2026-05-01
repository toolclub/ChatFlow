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


# ── 同步 IO（被 asyncio.to_thread 包装） ─────────────────────────────────────

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
    size = await asyncio.to_thread(_sync_write_df, path, df)
    logger.info("spot 写盘 market=%s day=%s rows=%d size=%dKB",
                market, (day or date.today()), len(df), size // 1024)
    return size


async def read_spot(market: str, day: date | None = None) -> pd.DataFrame | None:
    return await asyncio.to_thread(_sync_read_df, spot_path(market, day))


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
    """按 date 列拆分写入每日文件（一只股票多日的 long 格式 → 按日分桶）。"""
    if df is None or df.empty or "date" not in df.columns:
        return 0

    def _split_and_write() -> int:
        total = 0
        days = pd.to_datetime(df["date"]).dt.date.unique()
        for d in days:
            sub = df[pd.to_datetime(df["date"]).dt.date == d]
            if sub.empty:
                continue
            total += _sync_write_df(bars_path(market, d), sub.reset_index(drop=True))
        return total

    size = await asyncio.to_thread(_split_and_write)
    logger.info("bars 写盘 market=%s days=%d rows=%d size=%dKB",
                market, df["date"].nunique(), len(df), size // 1024)
    return size


async def read_bars_for_symbol(
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> pd.DataFrame | None:
    """读取单只标的在 [start, end] 区间内的所有缓存 bars（纯读盘，不判断覆盖率）。"""
    def _read() -> pd.DataFrame | None:
        bars_dir = _root() / "bars"
        frames = []
        for p in sorted(bars_dir.glob(f"{market}_*.pkl.gz")):
            d = _parse_file_date(p)
            if d is None or d < start or d > end:
                continue
            sub = _sync_read_df(p)
            if sub is not None and "symbol" in sub.columns and not sub.empty:
                match = sub[sub["symbol"].astype(str) == symbol]
                if not match.empty:
                    frames.append(match)
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
    return await asyncio.to_thread(_read)


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
                frames.append(sub)

        # 缺失的日期（实际历史交易日由调用方自行判断）
        present = set(avail.keys())
        all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        missing = [d for d in all_days if d not in present]

        if not frames:
            return None, missing
        return pd.concat(frames, ignore_index=True), missing

    return await asyncio.to_thread(_scan)


async def write_index(index_code: str, symbols: list[str]) -> int:
    if not symbols:
        return 0
    df = pd.DataFrame({"symbol": list(symbols)})
    size = await asyncio.to_thread(_sync_write_df, index_path(index_code), df)
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
    df = await asyncio.to_thread(_sync_read_df, p)
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
    return await asyncio.to_thread(_read)


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
    await asyncio.to_thread(_update)


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

    stats = await asyncio.to_thread(_prune)
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

    base = await asyncio.to_thread(_status)
    base["meta"] = await read_meta()
    return base


# ── 调用方语义糖：默认 lookback 区间 ─────────────────────────────────────────

def default_bars_range(end: date | None = None) -> tuple[date, date]:
    e = end or date.today()
    s = e - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))
    return s, e
