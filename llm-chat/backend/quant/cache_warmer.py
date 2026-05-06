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
_LOCK_TTL_SECONDS = 1800  # 全局会话锁 30 分钟（覆盖全市场 bars 最长耗时）
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
        self._first_tick = True  # 启动后首次进入循环强制跑一次

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
        # 释放 master 锁，避免下次启动被旧锁阻塞
        await _release_master_lock()
        logger.info("warmer 已停止")

    async def trigger_now(self, kinds: list[str] | None = None) -> dict:
        """REST 手动触发：在独立进程跑一次完整刷新，立即返回 'scheduled'。"""
        # 冷却期保护：基于 Redis 的全局冷却
        try:
            from db.redis_state import _get_redis
            r = _get_redis()
            last_ts = await r.get("chatflow:quant:last_refresh_ts")
            if last_ts:
                try:
                    last_val = float(last_ts)
                except (ValueError, TypeError):
                    last_val = 0.0
                if (time.time() - last_val) < 300 and not kinds:
                    return {"status": "skipped", "reason": "global_cooldown_active", "worker": _WORKER_ID}
        except Exception:
            pass

        kinds = kinds or ["spot", "index", "bars", "prune"]
        from quant.worker import start_refresh_process
        start_refresh_process(kinds)
        return {"scheduled": kinds, "worker": _WORKER_ID}

    # ── 主循环 ──────────────────────────────────────────────────────────────

    async def _loop(self, initial_delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass

        logger.debug("warmer 循环开始运行 worker_id=%s", _WORKER_ID)

        # 初始等待：给 Provider 注册留出 5 秒
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass

        # 抢 master 锁：抢到就干，抢不到直接退出
        if not await self._try_acquire_master_lock():
            logger.info("非 master worker，退出 warmer worker_id=%s", _WORKER_ID)
            return

        logger.info("获得 master 锁，开始定时预热 worker_id=%s", _WORKER_ID)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("warmer tick 异常: %s", exc)

            # 续期 master 锁（_tick 可能阻塞数分钟，返回后立即续）
            await self._renew_master_lock()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                break
            except asyncio.TimeoutError:
                continue

    async def _renew_master_lock(self) -> None:
        """续期 master 锁，防止长耗时 _tick 导致锁过期。"""
        try:
            from db.redis_state import _get_redis  # type: ignore
            r = _get_redis()
            cur = await r.get(f"{_LOCK_KEY_PREFIX}:master")
            if cur == _WORKER_ID:
                await r.expire(f"{_LOCK_KEY_PREFIX}:master", 90)
        except Exception:
            pass

    async def _try_acquire_master_lock(self) -> bool:
        """尝试成为 master worker（Redis SETNX，TTL 600s）。

        检测旧容器残留锁：hostname 不匹配时强制接管。
        Redis 不可用时直接放行（单机模式）。
        """
        try:
            from db.redis_state import _get_redis  # type: ignore
            r = _get_redis()
            ok = await r.set(
                f"{_LOCK_KEY_PREFIX}:master", _WORKER_ID,
                nx=True, ex=90,
            )
            if ok:
                return True
            cur = await r.get(f"{_LOCK_KEY_PREFIX}:master")
            if cur == _WORKER_ID:
                await r.expire(f"{_LOCK_KEY_PREFIX}:master", 90)
                return True
            # 锁存在但 worker 不同 → 检测是否旧容器残留
            if cur and ":" in cur:
                cur_host = cur.rsplit(":", 1)[0]
                my_host = _WORKER_ID.rsplit(":", 1)[0]
                if cur_host != my_host:
                    logger.info("检测到旧容器 master 锁 (%s)，强制接管", cur[:40])
                    await r.set(f"{_LOCK_KEY_PREFIX}:master", _WORKER_ID, ex=90)
                    return True
            return False
        except Exception:
            return True

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
        if _is_trading_hours(now) or _is_us_trading_hours(now):
            need = (time.time() - last_spot_val) >= QUANT_WARMER_SPOT_INTERVAL
            if need or self._first_tick:
                # 30分钟一次：刷新实时行情 + Top 2000 活跃股 K 线
                # 注意：这里会静默尝试获取锁，抢不到就跳过，不会踢掉手动任务
                await self._refresh_once(["spot", "bars_top"])
                self._first_tick = False

        # 全量预热：凌晨跑 (4 AM)
        today = date.today()
        if (now.hour >= QUANT_WARMER_BARS_HOUR and self.last_bars_day_ok != today and not _is_weekend(today)):
            # 凌晨一次：同步指数清单 + 全市场 K 线 + 清理旧数据
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
        # 如果是定时触发，发现有锁直接跳过；如果是手动触发，发现有锁也告知冲突
        session_lock_token = await _acquire_lock("global_session")
        if not session_lock_token:
            logger.info("⏱️ [%s] 预热跳过：当前已有活跃预热任务在运行中", mode)
            return

        logger.info("⏱️ [%s] 🚀 开启全局独占预热任务 | Worker: %s | 计划: %s", mode, _WORKER_ID, kinds)
        start_round = time.perf_counter()

        # 标记预热进行中（前端 /cache/status 轮询用）
        warming_key = "chatflow:quant:warming"
        try:
            from db.redis_state import _get_redis
            r = _get_redis()
            await r.set(warming_key, f"{_WORKER_ID}:{','.join(kinds)}", ex=3600)
        except Exception:
            pass

        try:
            # 记录开始时间到 Redis 供全局冷却参考
            await r.set("chatflow:quant:last_refresh_ts", str(time.time()), ex=1800)

            for kind in kinds:
                t0 = time.perf_counter()
                logger.info("  ▶️ 阶段开始: %s", kind)
                try:
                    if kind == "spot":
                        await self._do_spot()
                    elif kind == "bars":
                        await self._do_bars()
                    elif kind == "bars_top":
                        await self._do_bars(top_n=2000)
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
            # 清除预热标记
            try:
                from db.redis_state import _get_redis
                await _get_redis().delete(warming_key)
            except Exception:
                pass
            # 只有任务彻底执行完（或崩溃）才释放总锁
            await _release_lock("global_session", session_lock_token)

    async def _do_spot(self) -> None:
        t0 = time.perf_counter()
        adapter = get_adapter()
        for market in ["cn_a", "us_stock"]:
            try:
                df = await adapter.spot(market)
                if df is not None and not df.empty:
                    logger.info("    ∟ Spot (%s) 更新完成 | 数量: %d", market, len(df))
            except Exception as exc:
                logger.warning("    ❌ Spot (%s) 失败: %s", market, exc)
        self.last_spot_ok = time.time()

    async def _do_bars(self, symbols: list[str] | None = None, top_n: int | None = None) -> None:
        """拉取 bars + 回溯 lookback 区间内缺失日期。"""
        if symbols:
            # 按需补仓：优先美股
            us_syms = [s for s in symbols if s.endswith(".US")]
            cn_syms = [s for s in symbols if not s.endswith(".US")]
            if us_syms: await self._do_bars_market("us_stock", us_syms)
            if cn_syms: await self._do_bars_market("cn_a", cn_syms)
        elif top_n:
            # 30分钟定时预热：优先美股 Top
            await self._do_bars_market("us_stock", top_n=top_n)
            await self._do_bars_market("cn_a", top_n=top_n)
        else:
            # 全量同步（手动或凌晨）：VIP 优先策略
            # 1. 先把两个市场的 Top 2000 刷了（保证核心票秒级就绪）
            logger.info("🚀 [VIP 优先] 开始同步两市核心标的...")
            await self._do_bars_market("us_stock", top_n=2000)
            await self._do_bars_market("cn_a", top_n=2000)
            
            # 2. 再补齐剩下的长尾标的
            logger.info("⏳ [长尾补全] 开始同步两市剩余标的...")
            await self._do_bars_market("us_stock")
            await self._do_bars_market("cn_a")

    async def _do_bars_market(self, market: str, symbols: list[str] | None = None, top_n: int | None = None) -> None:
        t0 = time.perf_counter()
        adapter = get_adapter()
        
        # 1. 确定标的清单并排序
        if symbols:
            # 手动补仓或按需补仓，保持原顺序或简单排序
            sym_list = sorted(list(set(symbols)))
        else:
            spot = await cache_disk.read_spot(market)
            if spot is None or spot.empty or not await cache_disk.is_spot_fresh(market):
                spot = await adapter.spot(market)
            if spot is None or spot.empty:
                logger.warning("warmer bars (%s): spot 为空，跳过", market)
                return
            
            # 无论是否限制 top_n，只要有 amount 字段，永远按成交额降序排列
            # 这确保了苹果、微软等大票永远在第一批被处理
            if "amount" in spot.columns:
                spot_sorted = spot.sort_values("amount", ascending=False)
                if top_n:
                    spot_sorted = spot_sorted.head(top_n)
                    logger.info("    ∟ [%s] 开启 Top %d 预热 (基于成交额)", market, len(spot_sorted))
                else:
                    logger.info("    ∟ [%s] 开启全市场预热 (已按成交额优先排序)", market)
                sym_list = spot_sorted["symbol"].astype(str).tolist()
            else:
                sym_list = sorted(spot["symbol"].astype(str).tolist())

        if not sym_list:
            return

        total_count = len(sym_list)
        logger.info("    ∟ [%s] 开始拉取 K 线 | 目标数量: %d", market, total_count)

        # 2. 分批拉取与写盘（每 200 只一组）
        # 解决“憋大招”问题：拉完一组存一组，让选股立刻能看到数据
        chunk_size = 200
        heartbeat_stop = asyncio.Event()

        async def _heartbeat():
            cnt = 0
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=60.0)
                    break
                except asyncio.TimeoutError:
                    cnt += 1
                    try:
                        from db.redis_state import _get_redis
                        r = _get_redis()
                        cur = await r.get(f"{_LOCK_KEY_PREFIX}:global_session")
                        if cur and _WORKER_ID.rsplit(":", 1)[0] in str(cur):
                            await r.expire(f"{_LOCK_KEY_PREFIX}:global_session", 1800)
                        cur_m = await r.get(f"{_LOCK_KEY_PREFIX}:master")
                        if cur_m == _WORKER_ID:
                            await r.expire(f"{_LOCK_KEY_PREFIX}:master", 600)
                    except Exception:
                        pass
                    logger.info("    ∟ [%s] 仍在拉取 K 线… 已耗时 ~%d min", market, cnt)

        heartbeat_task = asyncio.create_task(_heartbeat())
        end_d = date.today()
        start_d = end_d - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))
        
        success_count = 0
        try:
            for i in range(0, total_count, chunk_size):
                chunk = sym_list[i : i + chunk_size]
                try:
                    # adapter.bars 内部会自动写盘
                    df = await adapter.bars(symbols=chunk, start=start_d, end=end_d, market=market)
                    if df is not None and not df.empty:
                        chunk_ok = df["symbol"].nunique() if "symbol" in df.columns else 0
                        success_count += chunk_ok
                        logger.info("    ∟ [%s] 进度: %d/%d | 本批次入盘: %d 只", market, min(i + chunk_size, total_count), total_count, chunk_ok)
                    else:
                        logger.info("    ∟ [%s] 进度: %d/%d | 本批次无新数据", market, min(i + chunk_size, total_count), total_count)
                except Exception as e:
                    logger.warning("    ❌ [%s] 批次 %d-%d 拉取失败: %s", market, i, i + len(chunk), e)
            
            if success_count > 0:
                self.last_bars_day_ok = end_d
                logger.info("    ∟ [%s] Bars 抓取完成 | 总计成功: %d/%d | 总耗时: %.1fs",
                            market, success_count, total_count, time.perf_counter() - t0)
        finally:
            heartbeat_stop.set()
            if not heartbeat_task.done(): heartbeat_task.cancel()

    async def _do_index(self) -> None:
        adapter = get_adapter()
        ok = 0
        t0 = time.perf_counter()
        indices = [
            ("hs300", "cn_a"), ("zz500", "cn_a"),
            ("nasdaq_list", "us_stock"), ("sp500_list", "us_stock")
        ]
        for code, market in indices:
            try:
                syms = await adapter.index_constituents(code, market=market)
                if syms:
                    ok += 1
                    logger.info("    ∟ Index %s (%s) 加载成功 | 数量: %d", code, market, len(syms))
            except Exception as exc:
                logger.warning("warmer index %s (%s) 失败: %s", code, market, exc)
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

async def _release_master_lock() -> None:
    """释放当前 worker 持有的 master 锁。"""
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        cur = await r.get(f"{_LOCK_KEY_PREFIX}:master")
        if cur == _WORKER_ID:
            await r.delete(f"{_LOCK_KEY_PREFIX}:master")
    except Exception:
        pass


async def _acquire_lock(kind: str) -> str | None:
    """成功返回 token，失败返回 None。

    旧容器残留检测：锁持有者 hostname 不匹配当前容器时强制接管。
    Redis 不可用时直接放行（单机模式）。
    """
    token = f"{_WORKER_ID}:{int(time.time() * 1000)}"
    try:
        from db.redis_state import _get_redis  # type: ignore
        r = _get_redis()
        ok = await r.set(f"{_LOCK_KEY_PREFIX}:{kind}", token, nx=True, ex=_LOCK_TTL_SECONDS)
        if ok:
            return token
        # 锁已存在 → 检测是否旧容器残留
        cur = await r.get(f"{_LOCK_KEY_PREFIX}:{kind}")
        if cur and ":" in cur:
            cur_hostname = cur.split(":")[0]
            my_hostname = _WORKER_ID.split(":")[0]
            if cur_hostname != my_hostname:
                logger.info("检测到旧容器 %s 锁 (%s)，强制接管", kind, cur[:40])
                await r.set(f"{_LOCK_KEY_PREFIX}:{kind}", token, ex=_LOCK_TTL_SECONDS)
                return token
        return None
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


def _is_us_trading_hours(now: datetime) -> bool:
    """美股大致时间窗（北京时间）：21:30 - 04:00 (夏令时) / 22:30 - 05:00 (冬令时)"""
    if _is_weekend(now.date()):
        return False
    minute = now.hour * 60 + now.minute
    # 宽松范围：21:00 - 06:00
    return minute >= 21 * 60 or minute <= 6 * 60


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
