"""量化数据后台预热

设计：
  - 单 asyncio.Task 主循环，每 5min 检查一次"该刷什么"
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
    QUANT_TUSHARE_BULK_WORKERS,
    QUANT_WARMER_BARS_HOUR,
    QUANT_WARMER_ENABLED,
    QUANT_WARMER_INDEX_HOUR,
    QUANT_WARMER_ROLE,
    QUANT_WARMER_SPOT_INTERVAL,
)
from quant.domain import ProviderCapability
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
        # tushare bulk 冷却：同一轮内 VIP→长尾两次调用不要重复拉全市场
        self._last_bulk_cn_a: float = 0.0

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, initial_delay: float = 5.0) -> None:
        import sys
        print(f"[WARMER] start() called, enabled={QUANT_WARMER_ENABLED}, running={self.is_running()}", file=sys.stderr, flush=True)
        if not QUANT_WARMER_ENABLED:
            logger.info("warmer 未启用（QUANT_WARMER_ENABLED=false）")
            return
        # 分布式单一执行者：非 primary 角色直接退出
        if QUANT_WARMER_ROLE != "primary":
            logger.info("warmer 角色=%s，非 primary 不启动预热", QUANT_WARMER_ROLE)
            return
        if self.is_running():
            print(f"[WARMER] already running, returning", file=sys.stderr, flush=True)
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(initial_delay))
        logger.info("warmer 启动 worker_id=%s", _WORKER_ID)
        print(f"[WARMER] task created, entering _loop", file=sys.stderr, flush=True)

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
        asyncio.create_task(self._refresh_once(kinds, manual=True))
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

        # 防堆积：用 reentrancy 标记保证同一时间只有一个 _tick 在跑。
        # while 串行结构本身已串行执行，但加保护更稳（避免未来误改并发触发）。
        tick_running = False

        while not self._stop.is_set():
            if tick_running:
                logger.warning("warmer 上一轮 _tick 仍在跑，跳过本次唤醒（防堆积）")
            else:
                tick_running = True
                t0 = time.time()
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("warmer tick 异常: %s", exc)
                finally:
                    elapsed = time.time() - t0
                    if elapsed > QUANT_WARMER_SPOT_INTERVAL:
                        logger.warning(
                            "warmer _tick 耗时 %.0fs 超过 spot_interval %ds，"
                            "数据更新可能落后；考虑减小预热范围或加大间隔",
                            elapsed, QUANT_WARMER_SPOT_INTERVAL,
                        )
                    tick_running = False

            # 续期 master 锁（_tick 可能阻塞数分钟，返回后立即续）
            await self._renew_master_lock()

            # 主循环唤醒间隔：取 spot_interval 的 1/4，但下限 60s 上限 600s。
            # 例如 spot_interval=14400(4h) → 每 600s 唤醒检查一次（够及时）。
            poll = max(60, min(600, QUANT_WARMER_SPOT_INTERVAL // 4))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
                break
            except asyncio.TimeoutError:
                continue

    async def _renew_master_lock(self) -> None:
        """文件锁不需要续期，持有 fd 就会一直锁住。此处留空以兼容老代码调用。"""
        pass

    async def _try_acquire_master_lock(self) -> bool:
        """尝试获取文件独占锁，确保单机多 Worker 下只有一个预热循环运行。"""
        import fcntl
        import os
        from pathlib import Path
        
        lock_path = Path("/tmp/chatflow-quant-warmer-bg.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            # 把 fd 挂到 self 上防止被 gc 回收导致释放
            self._bg_lock_fd = fd
            return True
        except (BlockingIOError, OSError):
            return False

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

        # spot + bars_top：每 30 分钟无条件刷新（不区分交易时段/节假日）
        need = (time.time() - last_spot_val) >= QUANT_WARMER_SPOT_INTERVAL
        if need or self._first_tick:
            await self._refresh_once(["spot", "bars_top"])
            self._first_tick = False

        # 全量预热：凌晨跑 (4 AM)
        today = date.today()
        if (now.hour >= QUANT_WARMER_BARS_HOUR and self.last_bars_day_ok != today and not _is_weekend(today)):
            # 凌晨一次：同步指数清单 + 全市场 K 线 + 清理旧数据
            await self._refresh_once(["index", "bars", "prune"])
        
        if (now.hour >= QUANT_WARMER_INDEX_HOUR and self.last_index_day_ok != today):
            await self._refresh_once(["index"])

        # 定时清理可能异常中断的卡死选股任务（超过 15 分钟）
        if now.minute % 5 == 0:  # 每 5 分钟尝试清理一次
            try:
                from db.quant_store import cleanup_stale_quant_sessions
                cleaned = await cleanup_stale_quant_sessions(timeout_minutes=15)
                if cleaned > 0:
                    logger.info("已清理 %d 个超时的异常选股任务", cleaned)
            except Exception as exc:
                logger.error("清理超时选股任务失败: %s", exc)

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

            # 单阶段总超时（兜底）：bars 全市场 25 min；其它 5 min。
            # 任何阶段卡死都不会拖垮整轮，下一轮 30min tick 会重试。
            phase_timeout = {"bars": 1500.0, "bars_top": 600.0}
            for kind in kinds:
                t0 = time.perf_counter()
                logger.info("  ▶️ 阶段开始: %s", kind)
                timeout_s = phase_timeout.get(kind, 300.0)
                try:
                    if kind == "spot":
                        await asyncio.wait_for(self._do_spot(), timeout=timeout_s)
                    elif kind == "bars":
                        await asyncio.wait_for(self._do_bars(), timeout=timeout_s)
                    elif kind == "bars_top":
                        await asyncio.wait_for(self._do_bars(top_n=2000), timeout=timeout_s)
                    elif kind == "index":
                        await asyncio.wait_for(self._do_index(), timeout=timeout_s)
                    elif kind == "prune":
                        await asyncio.wait_for(cache_disk.prune(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.warning("  ⏱️ 阶段超时跳过: %s | 上限: %.0fs", kind, timeout_s)
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
        # force=True 强制回源，不走缓存 freshness 检查
        for market in ["cn_a", "us_stock"]:
            try:
                df = await adapter.spot(market, force=True)
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

        # 2.0 准备日期窗口（bulk 与回退路径共用）
        end_d = date.today()
        start_d = end_d - timedelta(days=int(QUANT_BARS_LOOKBACK_DAYS * 1.6))

        # 2.1 cn_a 快速路径：akshare 并发优先（免费、~3min 全覆盖）
        #     → tushare bulk 退路（积分 ≥2000 时 ~30s 全覆盖）
        #     → 都失败再走 2.2 单点回退（baostock + 60s 超时）
        # 只对计划性全量/Top 预热触发；按需补仓（指定 symbols）不走 bulk，避免小请求触发全市场拉取
        if market == "cn_a" and symbols is None:
            covered = await self._bulk_fetch_cn_a(sym_list, start_d, end_d)
            if covered:
                covered_set = set(covered)
                sym_list = [s for s in sym_list if s not in covered_set]
                if not sym_list:
                    self.last_bars_day_ok = end_d
                    logger.info(
                        "    ∟ [%s] Bars 抓取完成（bulk 全覆盖） | 总耗时: %.1fs",
                        market, time.perf_counter() - t0,
                    )
                    return
                logger.info(
                    "    ∟ [%s] bulk 已覆盖 %d 只，剩余 %d 只走单点回退",
                    market, len(covered), len(sym_list),
                )
                total_count = len(sym_list)

        # 2.2 回退路径：分批走 registry（baostock per-symbol with 超时 / akshare 并发）
        # chunk_size 缩小到 50，单批被卡死时损失更可控
        chunk_size = 50
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

        # 单批 watchdog：超过 5 分钟视为该批被卡死，跳过继续
        CHUNK_TIMEOUT = 300.0
        success_count = 0
        try:
            for i in range(0, total_count, chunk_size):
                chunk = sym_list[i : i + chunk_size]
                try:
                    df = await asyncio.wait_for(
                        adapter.bars(symbols=chunk, start=start_d, end=end_d, market=market),
                        timeout=CHUNK_TIMEOUT,
                    )
                    if df is not None and not df.empty:
                        chunk_ok = df["symbol"].nunique() if "symbol" in df.columns else 0
                        success_count += chunk_ok
                        logger.info("    ∟ [%s] 进度: %d/%d | 本批次入盘: %d 只", market, min(i + chunk_size, total_count), total_count, chunk_ok)
                    else:
                        logger.info("    ∟ [%s] 进度: %d/%d | 本批次无新数据", market, min(i + chunk_size, total_count), total_count)
                except asyncio.TimeoutError:
                    logger.warning("    ❌ [%s] 批次 %d-%d 超时 %.0fs，跳过",
                                   market, i, i + len(chunk), CHUNK_TIMEOUT)
                except Exception as e:
                    logger.warning("    ❌ [%s] 批次 %d-%d 拉取失败: %s", market, i, i + len(chunk), e)

            if success_count > 0:
                self.last_bars_day_ok = end_d
                logger.info("    ∟ [%s] Bars 抓取完成 | 总计成功: %d/%d | 总耗时: %.1fs",
                            market, success_count, total_count, time.perf_counter() - t0)
        finally:
            heartbeat_stop.set()
            if not heartbeat_task.done(): heartbeat_task.cancel()

    async def _bulk_fetch_cn_a(
        self, sym_list: list[str], start_d: date, end_d: date,
    ) -> set[str]:
        """A 股全市场快速预热的总编排：akshare 优先 → tushare bulk 兜底。

        - akshare：免 token、HTTP 并发 16，~2-3min 全覆盖；只要不是 DOWN 就先试它
        - tushare bulk：要 ≥2000 积分；积分够就 30s 全覆盖，是更快的极速档
        - 都失败 → 返回空集，调用方走单点 chunk 回退（baostock + 60s 超时）
        """
        # 冷却：一轮 _do_bars 里 VIP-Top 与全量长尾会先后各调一次，第二次直接报告全覆盖
        if time.time() - self._last_bulk_cn_a < 600:
            logger.info(
                "    ∟ [cn_a] bulk 冷却中（<10min），跳过重复拉取，假定全覆盖",
            )
            return set(sym_list)

        # 1) akshare 并发优先
        covered = await self._bulk_via_akshare(sym_list, start_d, end_d)
        if covered:
            return covered

        # 2) tushare bulk 兜底（积分够时极速档）
        return await self._bulk_via_tushare(sym_list, start_d, end_d)

    async def _bulk_via_akshare(
        self, sym_list: list[str], start_d: date, end_d: date,
    ) -> set[str]:
        """akshare 并发拉 A 股全市场 daily_bars，分批写盘控制内存峰值。"""
        try:
            registry = get_registry()
            ak_candidates = registry.candidates(
                ProviderCapability.DAILY_BARS, market="cn_a",
            )
        except Exception as exc:
            logger.debug("registry 不可用，跳过 akshare bulk: %s", exc)
            return set()
        ak = next((p for p in ak_candidates if p.name == "akshare"), None)
        if ak is None:
            return set()

        t0 = time.perf_counter()
        # 分批写盘，避免 5500 个 DataFrame 全在内存里 concat
        BATCH = 1000
        BATCH_TIMEOUT = 240.0  # 1000 只 / 16 并发 ≈ 25s，给 4 分钟兜底
        covered: set[str] = set()
        total_rows = 0
        sym_set = set(sym_list)

        logger.info(
            "    ∟ [cn_a] 🚀 akshare 并发预热 | 目标 %d 只 | 分批 %d × %d 批",
            len(sym_list), BATCH, (len(sym_list) + BATCH - 1) // BATCH,
        )
        for i in range(0, len(sym_list), BATCH):
            batch = sym_list[i : i + BATCH]
            try:
                df = await asyncio.wait_for(
                    ak.daily_bars(
                        batch,
                        start=start_d.strftime("%Y-%m-%d"),
                        end=end_d.strftime("%Y-%m-%d"),
                    ),
                    timeout=BATCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "    ❌ [cn_a] akshare 批次 %d-%d 超时 %.0fs，停止 akshare 路径",
                    i, i + len(batch), BATCH_TIMEOUT,
                )
                break
            except Exception as exc:
                logger.warning(
                    "    ❌ [cn_a] akshare 批次 %d-%d 异常: %s，停止 akshare 路径",
                    i, i + len(batch), exc,
                )
                break

            if df is None or df.empty or "symbol" not in df.columns:
                continue
            try:
                await cache_disk.write_bars("cn_a", df)
            except Exception as exc:
                logger.warning("    ❌ [cn_a] akshare 批次 %d 写盘失败: %s", i, exc)
                continue

            new_covered = set(df["symbol"].astype(str).unique()) & sym_set
            covered.update(new_covered)
            total_rows += len(df)
            logger.info(
                "    ∟ [cn_a] akshare 进度 %d/%d | 累计覆盖 %d 只 | 累计行数 %d",
                min(i + BATCH, len(sym_list)), len(sym_list),
                len(covered), total_rows,
            )

        # 至少覆盖一半才算成功；否则让 tushare/单点回退接力
        if covered and len(covered) >= len(sym_list) * 0.5:
            self._last_bulk_cn_a = time.time()
            logger.info(
                "    ∟ [cn_a] ✅ akshare bulk 完成 | 覆盖 %d/%d | 行数 %d | 耗时: %.1fs",
                len(covered), len(sym_list), total_rows, time.perf_counter() - t0,
            )
            return covered
        if covered:
            logger.warning(
                "    ⚠️ [cn_a] akshare 覆盖率不足（%d/%d <50%%），尝试 tushare 兜底",
                len(covered), len(sym_list),
            )
        return covered  # 部分覆盖也返回，调用方会跳过这部分继续 fallback

    async def _bulk_via_tushare(
        self, sym_list: list[str], start_d: date, end_d: date,
    ) -> set[str]:
        """tushare 按交易日并发批量拉 A 股全市场 daily_bars。

        要求积分 ≥2000（pro.daily/adj_factor 的 trade_date 模式门槛）。
        120 积分账号会被服务端拒，daily_bars 能力会被 health_check 摘掉，本方法直接跳过。
        """
        try:
            registry = get_registry()
            tushare = registry.get_provider("tushare")
        except Exception as exc:
            logger.debug("registry 不可用，跳过 tushare bulk: %s", exc)
            return set()
        if tushare is None:
            return set()
        if ProviderCapability.DAILY_BARS not in getattr(tushare, "capabilities", set()):
            logger.info("    ∟ [cn_a] tushare 无 daily_bars 能力（积分不足），跳过 bulk 路径")
            return set()
        if not hasattr(tushare, "daily_bars_bulk"):
            return set()

        t0 = time.perf_counter()
        logger.info(
            "    ∟ [cn_a] 🚀 tushare bulk-by-date 开始 | %s ~ %s | 并发 %d",
            start_d, end_d, QUANT_TUSHARE_BULK_WORKERS,
        )
        try:
            df = await asyncio.wait_for(
                tushare.daily_bars_bulk(
                    start=start_d.strftime("%Y-%m-%d"),
                    end=end_d.strftime("%Y-%m-%d"),
                    max_workers=QUANT_TUSHARE_BULK_WORKERS,
                ),
                timeout=360.0,
            )
        except asyncio.TimeoutError:
            logger.warning("    ❌ [cn_a] tushare bulk 超时（>360s），降级回退路径")
            return set()
        except Exception as exc:
            logger.warning("    ❌ [cn_a] tushare bulk 失败: %s（降级回退路径）", exc)
            return set()

        if df is None or df.empty or "symbol" not in df.columns:
            logger.info("    ∟ [cn_a] tushare bulk 无数据返回")
            return set()

        try:
            await cache_disk.write_bars("cn_a", df)
        except Exception as exc:
            logger.warning("    ❌ [cn_a] tushare bulk 写盘失败: %s", exc)
            return set()

        covered = set(df["symbol"].astype(str).unique()) & set(sym_list)
        self._last_bulk_cn_a = time.time()
        logger.info(
            "    ∟ [cn_a] ✅ tushare bulk 完成 | 覆盖 %d/%d | 行数 %d | 耗时: %.1fs",
            len(covered), len(sym_list), len(df), time.perf_counter() - t0,
        )
        return covered

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
