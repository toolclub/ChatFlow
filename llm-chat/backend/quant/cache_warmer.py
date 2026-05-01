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
_PENDING_WARM_KEY = "chatflow:quant:pending_warm"

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
        # 冷却期保护：基于 Redis 的全局冷却
        try:
            from db.redis_state import _get_redis
            r = _get_redis()
            last_ts = await r.get("chatflow:quant:last_refresh_ts")
            if last_ts and (time.time() - float(last_ts)) < 300 and not kinds:
                 return {"status": "skipped", "reason": "global_cooldown_active", "worker": _WORKER_ID}
        except Exception:
            pass

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
        
        # 初始等待：给 Provider 注册留出 45 秒
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=45.0)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            # 核心原则：真正分布式单活计算。
            is_master = await self._try_acquire_master_lock()
            
            if is_master:
                # 只有 Master 才会在空闲时执行自动 tick
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("warmer tick 异常: %s", exc)
            
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                break
            except asyncio.TimeoutError:
                continue

    # ... _try_acquire_master_lock 保持不变 ...

    async def _tick(self) -> None:
        now = datetime.now()
        # 读 Redis 获取全局最后的成功时间
        try:
            from db.redis_state import _get_redis
            r = _get_redis()
            last_spot = await r.get("chatflow:quant:last_spot_ok")
            last_spot_val = float(last_spot) if last_spot else 0.0
        except Exception:
            last_spot_val = self.last_spot_ok

        # spot：行情时间窗 + 间隔到了
        if _is_trading_hours(now):
            need = (time.time() - last_spot_val) >= QUANT_WARMER_SPOT_INTERVAL
            if need:
                await self._refresh_once(["spot"])

        # bars/index 逻辑同理...（由于已持有 Master 锁，这里会自动单活）
        today = date.today()
        if (now.hour >= QUANT_WARMER_BARS_HOUR and self.last_bars_day_ok != today and not _is_weekend(today)):
            await self._refresh_once(["index", "bars", "prune"])
        if (now.hour >= QUANT_WARMER_INDEX_HOUR and self.last_index_day_ok != today):
            await self._refresh_once(["index"])

        # 按需补仓：处理 API 路径发现的缓存缺失标的
        await self._warm_on_demand()

    # ── 实际刷新（全局会话大锁） ────────────────────────────────────────────────

    async def _refresh_once(self, kinds: list[str], manual: bool = False) -> None:
        """核心重构：实现全流程大铁锁，彻底杜绝接力抢任务。"""
        mode = "手动" if manual else "自动"
        
        # 1. 尝试获取全局会话大锁（整个预热过程只能有 1 个 Worker 活跃）
        session_lock_token = await _acquire_lock("global_session")
        if not session_lock_token:
            logger.debug("⏱️ [%s] 预热跳过：另一个 Worker 正在执行全局任务", mode)
            return

        logger.info("⏱️ [%s] 🚀 开启全局独占预热任务 | Worker: %s | 计划: %s", mode, _WORKER_ID, kinds)
        start_round = time.perf_counter()
        
        try:
            # 记录开始时间到 Redis 供全局冷却参考
            from db.redis_state import _get_redis
            r = _get_redis()
            await r.set("chatflow:quant:last_refresh_ts", str(time.time()), ex=1800)

            for kind in kinds:
                t0 = time.perf_counter()
                logger.info("  ▶️ 阶段开始: %s", kind)
                try:
                    if kind == "spot":
                        await self._do_spot()
                    elif kind == "bars":
                        await self._do_bars()
                    elif kind == "index":
                        await self._do_index()
                    elif kind == "prune":
                        await cache_disk.prune()
                except Exception as exc:
                    logger.exception("  ❌ 阶段失败: %s | %s", kind, exc)
                
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("  ✅ 阶段完成: %s | 耗时: %.0fms", kind, elapsed)

            total_elapsed = (time.perf_counter() - start_round)
            logger.info("🏁 [%s] 全局预热任务结束 | 总耗时: %.1fs", mode, total_elapsed)
            
            # 成功后更新 Redis 全局标记
            await r.set("chatflow:quant:last_spot_ok", str(time.time()), ex=86400)
            self.last_spot_ok = time.time()

        finally:
            # 2. 只有任务彻底执行完（或崩溃）才释放总锁
            await _release_lock("global_session", session_lock_token)

    async def _do_spot(self) -> None:
        t0 = time.perf_counter()
        adapter = get_adapter()
        df = await adapter.spot("cn_a")
        if df is not None and not df.empty:
            self.last_spot_ok = time.time()
            logger.info("    ∟ Spot 数据更新完成 | 数量: %d | 内部耗时: %.0fms", len(df), (time.perf_counter() - t0) * 1000)

    async def _do_bars(self, symbols: list[str] | None = None) -> None:
        """拉取 bars + 回溯 lookback 区间内缺失日期。

        不传 symbols 时默认拉全市场所有标的（定时预热）；
        传 symbols 时仅拉指定标的（按需补仓）。
        """
        t0 = time.perf_counter()
        adapter = get_adapter()

        if symbols:
            syms = set(symbols)
        else:
            # 全市场：从 spot 拿全部 symbol
            spot = await cache_disk.read_spot("cn_a")
            if spot is None or spot.empty:
                spot = await adapter.spot("cn_a")
            if spot is None or spot.empty:
                logger.warning("warmer bars: spot 为空，跳过")
                return
            syms = set(spot["symbol"].astype(str))
            logger.info("    ∟ Bars 准备阶段完成 | 全市场 %d 只 | 耗时: %.0fms",
                        len(syms), (time.perf_counter() - t0) * 1000)

        if not syms:
            return

        t1 = time.perf_counter()
        logger.info("    ∟ 开始拉取 K 线 | 目标数量: %d", len(syms))

        end_d = date.today()
        start_d = end_d - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))

        df = await adapter.bars(
            symbols=sorted(syms),
            start=start_d,
            end=end_d,
        )
        if df is not None and not df.empty:
            self.last_bars_day_ok = end_d
            sym_count = df["symbol"].nunique() if "symbol" in df.columns else 0
            await cache_disk.update_meta({
                "bars_last_refresh": int(datetime.now().timestamp()),
                "bars_last_day": end_d.isoformat(),
                "bars_universe_size": int(sym_count),
            })
            logger.info("    ∟ Bars 抓取写入成功 | 数据量: %d | 覆盖: %d 只 | 耗时: %.1fs",
                        len(df), sym_count, time.perf_counter() - t1)

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


    async def _warm_on_demand(self) -> None:
        """处理 API 路径发现的缓存缺失标的（Redis SET 取一批 → 拉 bars 写盘）。"""
        try:
            from db.redis_state import _get_redis  # type: ignore
            r = _get_redis()
            # SPOP 最多 30 只，避免单次耗时过长
            syms: list[str] = []
            for _ in range(30):
                sym = await r.spop(_PENDING_WARM_KEY)
                if sym:
                    syms.append(sym)
                else:
                    break
            if not syms:
                return
        except Exception:
            return

        logger.info("🔥 按需补仓 %d 只标的", len(syms))
        try:
            await self._do_bars(symbols=syms)
        except Exception as exc:
            logger.warning("按需补仓失败: %s", exc)
            # 失败的放回队列，下次重试
            try:
                from db.redis_state import _get_redis
                r = _get_redis()
                await r.sadd(_PENDING_WARM_KEY, *syms)
            except Exception:
                pass


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


# ── 按需补仓（API 路径调用，非阻塞） ────────────────────────────────────────


async def request_warm(symbols: list[str]) -> bool:
    """API 发现缓存缺失时触发后台补仓（SADD 到 Redis，warmer 下次 tick 处理）。"""
    if not symbols:
        return False
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        added = await r.sadd(_PENDING_WARM_KEY, *symbols)
        if added:
            logger.info("按需补仓已入队 symbols=%s count=%d", symbols[:5], len(symbols))
        return bool(added)
    except Exception as exc:
        logger.debug("按需补仓入队失败（Redis 不可用）: %s", exc)
        return False


# ── 单例 ────────────────────────────────────────────────────────────────────

_state: WarmerState | None = None


def get_warmer() -> WarmerState:
    global _state
    if _state is None:
        _state = WarmerState()
    return _state
