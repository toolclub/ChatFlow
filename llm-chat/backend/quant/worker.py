"""
量化独立进程 Worker

为了防止量化选股（Pandas CPU密集型、大量同步网络请求）阻塞主 FastAPI 事件循环，
我们将选股任务和预热任务放到独立的进程中执行。每个进程拥有自己独立的 Event Loop。
"""
import asyncio
import logging
import multiprocessing
from multiprocessing import Process

logger = logging.getLogger("quant.worker")

def _init_worker_env():
    """初始化子进程的环境（日志、数据库等）"""
    from config import LOG_DIR, DATABASE_URL
    from logging_config import setup_logging
    from db.database import init_engine
    
    setup_logging(LOG_DIR)
    init_engine(DATABASE_URL)


# ── 选股进程 ─────────────────────────────────────────────────────────────

def _run_screen_sync(snapshot_id: str, client_id: str, criteria: dict, user_id: str):
    """选股独立进程入口点"""
    _init_worker_env()
    
    async def _main():
        from quant.bootstrap import init_quant
        await init_quant()
        
        from graph.quant_agent import background_screen
        await background_screen(snapshot_id, client_id, criteria, user_id)
        
        # 稍微延迟退出，确保底层网络连接（如 aiohttp）能正常关闭
        await asyncio.sleep(0.5)
        
    asyncio.run(_main())

def start_screen_process(snapshot_id: str, client_id: str, criteria: dict, user_id: str) -> Process:
    """启动选股独立进程"""
    p = Process(
        target=_run_screen_sync,
        args=(snapshot_id, client_id, criteria, user_id),
        daemon=True,
        name=f"QuantScreen-{snapshot_id[:8]}"
    )
    p.start()
    return p


# ── 预热常驻进程 ─────────────────────────────────────────────────────────

def _run_warmer_sync():
    """预热常驻进程入口点"""
    _init_worker_env()
    
    async def _main():
        from quant.bootstrap import init_quant
        await init_quant()
        
        from quant.cache_warmer import get_warmer
        warmer = get_warmer()
        await warmer.start(initial_delay=2.0)
        
        # 阻塞进程，保持 warmer 的 loop 持续运行
        try:
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            await warmer.stop(timeout=5.0)
            
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

def start_warmer_process() -> Process:
    """启动预热独立进程"""
    p = Process(
        target=_run_warmer_sync,
        daemon=True,
        name="QuantWarmer"
    )
    p.start()
    return p


# ── 手动刷新进程 ─────────────────────────────────────────────────────────

def _run_refresh_sync(kinds: list[str] | None):
    """手动刷新独立进程入口点"""
    _init_worker_env()
    
    async def _main():
        from quant.bootstrap import init_quant
        await init_quant()
        
        from quant.cache_warmer import get_warmer
        warmer = get_warmer()
        # 作为独立进程被触发，手动执行一次 refresh_once
        await warmer._refresh_once(kinds or ["spot", "index", "bars", "prune"], manual=True)
        
        await asyncio.sleep(0.5)
        
    asyncio.run(_main())

def start_refresh_process(kinds: list[str] | None) -> Process:
    """启动手动刷新独立进程"""
    p = Process(
        target=_run_refresh_sync,
        args=(kinds,),
        daemon=True,
        name="QuantRefresh"
    )
    p.start()
    return p
