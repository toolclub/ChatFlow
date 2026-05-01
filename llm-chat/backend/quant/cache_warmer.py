"""量化数据后台预热

设计：
  - 单 asyncio.Task 主循环，每 60s 检查一次"该刷什么"
  - 多 worker 互斥：通过 Redis SETNX 抢锁；抢不到就观察
  - 行情时间窗（9:15-15:30）每 N 分钟刷 spot
  - 收盘后（默认 16:00）刷当日 bars + 滚窗 prune
  - 开盘前（默认 7:00）刷指数成分
  - 启动后延迟 5s 触发首次预热（不阻塞 lifespan）

不做的事：
  - 不在主循环里"堵塞"几十秒去拉数据 — 拉数据是 await 同时其他 worker 看锁
  - 不持久化任务状态：进程崩溃重启会重新规划，幂等
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import date, datetime, timedelta

from quant import cache_disk
from quant.config import (
    QUANT_BARS_LOOKBACK_DAYS,
    QUANT_WARMER_BARS_HOUR,
    QUANT_WARMER_ENABLED,
    QUANT_WARMER_INDEX_HOUR,
    QUANT_WARMER_SPOT_INTERVAL,
)
from quant.data_adapter import get_adapter
from quant.provider_registry import NoProviderAvailable, get_registry

logger = logging.getLogger("quant.timer")

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_LOCK_KEY_PREFIX = "chatflow:quant:warmer_lock"
_LOCK_TTL_SECONDS = 600

_INDEX_CODES_TO_WARM = ["hs300", "zz500"]


class WarmerState:
    """主循环状态（避免太多模块级全局）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 上次刷新成功的时间戳
        self.last_spot_ok: float = 0.0
        self.last_bars_day_ok: date | None = None
        self.last_index_day_ok: date | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, initial_delay: float = 5.0) -> None:
        if not QUANT_WARMER_ENABLED:
            logger.info("warmer 未启用（QUANT_WARMER_ENABLED=false）")
            return
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(initial_delay))
        logger.info("warmer 启动 worker_id=%s", _WORKER_ID)

    async def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running():
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("warmer 已停止")

    async def trigger_now(self, kinds: list[str] | None = None) -> dict:
        """REST 手动触发：在后台跑一次完整刷新，立即返回 'scheduled'。"""
        # 冷却期保护：防止 5 分钟内重复触发
        now = time.time()
        if now - self.last_spot_ok < 300 and not kinds:
            return {"status": "skipped", "reason": "cooldown_active", "worker": _WORKER_ID}

        kinds = kinds or ["spot", "index", "bars", "prune"]
        asyncio.create_task(self._refresh_once(kinds, manual=True))
        return {"scheduled": kinds, "worker": _WORKER_ID}

    # ── 主循环 ──────────────────────────────────────────────────────────────

    async def _loop(self, initial_delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass

        logger.info("warmer 循环开始运行 worker_id=%s", _WORKER_ID)

        while not self._stop.is_set():
            # 核心原则：真正分布式单活计算。
            # 只有抢到 master 锁的 worker 才会执行 _tick（含初始化首次预热）。
            is_master = await self._try_acquire_master_lock()
            
            if is_master:
                # 如果这是本进程第一次成为 Master，先跑一次首次同步
                if self.last_spot_ok == 0:
                    try:
                        await self._refresh_once(["spot", "index"], manual=True)
                    except Exception as exc:
                        logger.warning("首次预热失败: %s", exc)

                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("warmer tick 异常: %s", exc)
            else:
                logger.debug("当前 worker [%s] 不是 Master，跳过本轮 tick", _WORKER_ID)

            try:
                # 即使不是 Master，也每 60s 检查一次，随时准备接管
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                return
            except asyncio.TimeoutError:
                continue

    async def _try_acquire_master_lock(self) -> bool:
        """尝试成为全局量化计算 Master (Redis 选举)。"""
        from cache.factory import get_redis
        redis = get_redis()
        if redis is None:
            return True # 无 Redis 环境退化为单机多活

        lock_key = "quant:warmer:master"
        # 锁有效期 300 秒，主循环 60 秒跑一次，足以覆盖慢速拉取
        try:
            ok = await redis.set(lock_key, _WORKER_ID, nx=True, ex=300)
            if ok:
                return True
            
            val = await redis.get(lock_key)
            if val == _WORKER_ID:
                await redis.expire(lock_key, 300)
                return True
                
            return False
        except Exception as exc:
            logger.warning("Master 锁选举异常: %s", exc)
            return True # 异常时保守处理，允许各干各的（有 kind 级锁兜底）

    async def _tick(self) -> None:
        now = datetime.now()

        # spot：行情时间窗 + 间隔到了
        if _is_trading_hours(now):
            need = (time.time() - self.last_spot_ok) >= QUANT_WARMER_SPOT_INTERVAL
            if need:
                await self._refresh_once(["spot"])

        # bars：每天 BARS_HOUR 后还没刷过
        today = date.today()
        if (
            now.hour >= QUANT_WARMER_BARS_HOUR
            and self.last_bars_day_ok != today
            and not _is_weekend(today)  # 周末不动
        ):
            # 刷 bars 之前必须确保 index 已经就绪
            await self._refresh_once(["index", "bars", "prune"])

        # index：每天 INDEX_HOUR 后还没刷过
        if (
            now.hour >= QUANT_WARMER_INDEX_HOUR
            and self.last_index_day_ok != today
        ):
            await self._refresh_once(["index"])

    # ── 实际刷新（持锁执行） ────────────────────────────────────────────────

    async def _refresh_once(self, kinds: list[str], manual: bool = False) -> None:
        mode = "手动" if manual else "自动"
        logger.info("⏱️ [%s] 开启新一轮预热, 计划任务: %s", mode, kinds)
        start_round = time.perf_counter()
        
        for kind in kinds:
            lock_token = await _acquire_lock(kind)
            if not lock_token:
                logger.debug("warmer skip %s（锁被其他 worker 持有）", kind)
                continue
                
            t0 = time.perf_counter()
            logger.info("  ▶️ [%s] 启动阶段: %s", mode, kind)
            try:
                if kind == "spot":
                    await self._do_spot()
                elif kind == "bars":
                    await self._do_bars()
                elif kind == "index":
                    await self._do_index()
                elif kind == "prune":
                    await cache_disk.prune()
                else:
                    logger.warning("未知刷新类型 %s", kind)
            except NoProviderAvailable as exc:
                logger.warning("warmer %s: 无可用 provider — %s", kind, exc)
            except Exception as exc:
                logger.exception("warmer %s 失败: %s", kind, exc)
            finally:
                await _release_lock(kind, lock_token)
                
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("  ✅ [%s] 阶段完成: %s | 耗时: %.0fms", mode, kind, elapsed)

        total_elapsed = (time.perf_counter() - start_round)
        logger.info("⏱️ [%s] 全流程结束 | 总耗时: %.1fs", mode, total_elapsed)

    async def _do_spot(self) -> None:
        t0 = time.perf_counter()
        adapter = get_adapter()
        df = await adapter.spot("cn_a")
        if df is not None and not df.empty:
            self.last_spot_ok = time.time()
            logger.info("    ∟ Spot 数据更新完成 | 数量: %d | 内部耗时: %.0fms", len(df), (time.perf_counter() - t0) * 1000)

    async def _do_bars(self) -> None:
        """拉当日所有 bars + 回溯 lookback 区间内缺失日期。"""
        t0 = time.perf_counter()
        adapter = get_adapter()
        # 先拉 spot 拿到全市场 symbol
        spot = await cache_disk.read_spot("cn_a")
        if spot is None or spot.empty:
            spot = await adapter.spot("cn_a")
        if spot is None or spot.empty:
            logger.warning("warmer bars: spot 为空，跳过")
            return

        logger.info("    ∟ Bars 准备阶段完成 | 耗时: %.0fms", (time.perf_counter() - t0) * 1000)
        t1 = time.perf_counter()

        # 控制规模：太多股票走 bars 会非常慢，先拉 hs300 + zz500 做主力
        priority_syms: set[str] = set()
        for code in _INDEX_CODES_TO_WARM:
            cached = await cache_disk.read_index(code)
            if cached:
                priority_syms.update(cached)
        
        if not priority_syms:
            if "amount" in spot.columns:
                priority_syms = set(
                    spot.sort_values("amount", ascending=False)
                        .head(600)["symbol"].astype(str)
                )
            else:
                priority_syms = set(spot["symbol"].astype(str).head(600))

        logger.info("    ∟ 开始拉取核心标的 K 线 | 目标数量: %d", len(priority_syms))
        
        end_d = date.today()
        start_d = end_d - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))

        df = await adapter.bars(
            symbols=sorted(priority_syms),
            start=start_d,
            end=end_d,
        )
        if df is not None and not df.empty:
            self.last_bars_day_ok = end_d
            await cache_disk.update_meta({
                "bars_last_refresh": int(datetime.now().timestamp()),
                "bars_last_day": end_d.isoformat(),
                "bars_universe_size": len(priority_syms),
            })
            logger.info("    ∟ Bars 抓取写入成功 | 数据量: %d | 抓取耗时: %.1fs", len(df), time.perf_counter() - t1)

    async def _do_index(self) -> None:
        adapter = get_adapter()
        ok = 0
        t0 = time.perf_counter()
        for code in _INDEX_CODES_TO_WARM:
            try:
                syms = await adapter.index_constituents(code)
                if syms:
                    ok += 1
                    logger.info("    ∟ Index %s 加载成功 | 数量: %d", code, len(syms))
            except NoProviderAvailable:
                logger.warning("warmer index %s: 无 provider", code)
            except Exception as exc:
                logger.warning("warmer index %s 失败: %s", code, exc)
        if ok > 0:
            self.last_index_day_ok = date.today()
            await cache_disk.update_meta({
                "index_last_refresh": int(datetime.now().timestamp()),
                "index_last_day": date.today().isoformat(),
            })
            logger.info("    ∟ 指数清单更新完成 | 累计耗时: %.0fms", (time.perf_counter() - t0) * 1000)


# ── Redis 锁 ────────────────────────────────────────────────────────────────

async def _acquire_lock(kind: str) -> str | None:
    """成功返回 token，失败返回 None。Redis 不可用时直接放行（单机模式）。"""
    token = f"{_WORKER_ID}:{int(time.time() * 1000)}"
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        ok = await r.set(f"{_LOCK_KEY_PREFIX}:{kind}", token, nx=True, ex=_LOCK_TTL_SECONDS)
        return token if ok else None
    except Exception as exc:
        logger.debug("Redis 锁不可用，单机模式跑 %s: %s", kind, exc)
        return token


async def _release_lock(kind: str, token: str | None) -> None:
    if not token:
        return
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        # 仅当 token 仍是自己时才删（避免误删别人续期的锁）
        cur = await r.get(f"{_LOCK_KEY_PREFIX}:{kind}")
        if cur == token:
            await r.delete(f"{_LOCK_KEY_PREFIX}:{kind}")
    except Exception:
        pass


# ── 时间窗判定 ──────────────────────────────────────────────────────────────

def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _is_trading_hours(now: datetime) -> bool:
    """A 股大致时间窗：周一到周五 9:00-15:30（含集合竞价 / 午休）。
    严格交易日历由 provider 提供，warmer 这里宽松判断即可。"""
    if _is_weekend(now.date()):
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 <= minute <= 15 * 60 + 30


# ── 单例 ────────────────────────────────────────────────────────────────────

_state: WarmerState | None = None


def get_warmer() -> WarmerState:
    global _state
    if _state is None:
        _state = WarmerState()
    return _state
