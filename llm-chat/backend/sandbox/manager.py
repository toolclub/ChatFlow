"""
SandboxManager：沙箱 Worker 集群管理器

支持多 Worker 集群部署，只需修改 .env 配置：
  单机：  SANDBOX_WORKERS=[{"id":"w1","host":"host.docker.internal","port":2222,...}]
  集群：  SANDBOX_WORKERS=[{"id":"w1","host":"10.0.0.1","port":2222,...},{"id":"w2","host":"10.0.0.2","port":2222,...},{"id":"w3","host":"10.0.0.3","port":2222,...}]

集群能力：
  - 会话亲和性：同一 conv_id 始终路由到同一 Worker（保持文件上下文）
  - 负载均衡：新会话分配到活跃 session 最少的健康 Worker
  - 健康检查：每 30s 检测 Worker 连接，不可用自动摘除
  - 自动恢复：摘除的 Worker 恢复后自动重新加入
  - 故障转移：Worker 宕机时，新请求自动路由到其他 Worker
  - 定时清理：12h 未使用的 session 目录自动删除
"""
import asyncio
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any

from sandbox.worker import SSHWorker, ExecuteResult

logger = logging.getLogger("sandbox.manager")

SANDBOX_ROOT = "/sandbox"
_HEALTH_CHECK_INTERVAL = 30  # 秒

# Gunicorn 多 worker 下，只让持有此文件锁的那一个进程跑后台清理循环，
# 避免 N 份循环重复日志 + 浪费 CPU。
# 放在容器本地 tmpfs：① flock 跨进程语义可靠（VirtioFS 上 flock 不可靠）
# ② 容器重启自动清掉，不会留残留锁
_BG_LOCK_PATH = Path("/tmp/chatflow-sandbox-bg.lock")
_bg_lock_fd: int | None = None


def _try_acquire_bg_lock() -> bool:
    """文件独占锁：只有第一个 fork 出来的 worker 能拿到，其余立即返回 False。"""
    global _bg_lock_fd
    try:
        _BG_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_BG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        _bg_lock_fd = fd  # 保持句柄打开 = 一直持锁，进程退出自动释放
        return True
    except (BlockingIOError, OSError):
        return False


class SandboxManager:

    def __init__(self) -> None:
        # 所有已注册 Worker（含不健康的）
        self._all_workers: dict[str, SSHWorker] = {}
        self._worker_configs: dict[str, dict] = {}
        # 当前健康可用的 Worker
        self._healthy: set[str] = set()
        # conv_id → (worker_id, session_dir)
        self._sessions: dict[str, tuple[str, str]] = {}
        self._timeout: int = 30
        self._cleanup_hours: int = 12
        self._cleanup_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None

    async def init(
        self,
        workers_config: list[dict[str, Any]],
        timeout: int = 30,
        cleanup_hours: int = 12,
    ) -> None:
        self._timeout = timeout
        self._cleanup_hours = cleanup_hours

        for cfg in workers_config:
            worker_id = cfg.get("id", f"worker-{len(self._all_workers)}")
            self._worker_configs[worker_id] = cfg
            worker = SSHWorker(
                worker_id=worker_id,
                host=cfg["host"],
                port=cfg.get("port", 22),
                user=cfg.get("user", "root"),
                key_file=cfg.get("key_file", ""),
                password=cfg.get("password", ""),
            )
            self._all_workers[worker_id] = worker
            try:
                await worker.connect()
                await worker.exec_command(f"mkdir -p {SANDBOX_ROOT}")
                self._healthy.add(worker_id)
                logger.info(
                    "Sandbox Worker 上线 | id=%s | host=%s:%d",
                    worker_id, cfg["host"], cfg.get("port", 22),
                )
            except Exception as exc:
                logger.error("Sandbox Worker 连接失败 | id=%s | error=%s", worker_id, exc)

        if not self._healthy:
            logger.warning("无可用 Sandbox Worker，代码执行功能不可用")
            return

        # 健康检查 = 每个 gunicorn worker 各自维护自己的 SSH 连接（断线重连），
        # 必须每个进程都跑。清理 = 删远端过期 session 目录（幂等的文件系统操作），
        # 只需要一个进程跑就够，用文件独占锁抢到的那个负责。
        self._health_task = asyncio.create_task(self._health_check_loop())
        if _try_acquire_bg_lock():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                "SandboxManager 初始化完成（清理主进程）| pid=%d | total=%d | healthy=%d | timeout=%ds | cleanup=%dh",
                os.getpid(), len(self._all_workers), len(self._healthy), timeout, cleanup_hours,
            )
        else:
            logger.info(
                "SandboxManager 初始化完成（从进程，跳过清理循环）| pid=%d | total=%d | healthy=%d",
                os.getpid(), len(self._all_workers), len(self._healthy),
            )

    async def shutdown(self) -> None:
        for task in (self._health_task, self._cleanup_task):
            if task:
                task.cancel()
        for worker in self._all_workers.values():
            await worker.close()
        logger.info("SandboxManager 已关闭")

    @property
    def available(self) -> bool:
        return len(self._healthy) > 0

    def status(self) -> dict:
        """返回集群状态（供 API 接口查看）。"""
        return {
            "total_workers": len(self._all_workers),
            "healthy_workers": len(self._healthy),
            "active_sessions": len(self._sessions),
            "workers": [
                {
                    "id": wid,
                    "host": self._worker_configs.get(wid, {}).get("host", ""),
                    "port": self._worker_configs.get(wid, {}).get("port", 22),
                    "healthy": wid in self._healthy,
                    "sessions": sum(
                        1 for _, (w, _) in self._sessions.items() if w == wid
                    ),
                }
                for wid in self._all_workers
            ],
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Worker 调度
    # ══════════════════════════════════════════════════════════════════════════

    async def _get_worker_for_session(self, conv_id: str) -> tuple[SSHWorker, str]:
        """
        会话亲和 + DB 持久化 + 故障转移 + 最少连接负载均衡。

        查找顺序：
          1. 本地缓存 _sessions（热路径，无 IO）
          2. DB conversations.sandbox_worker_id（跨 worker 恢复）
          3. 负载均衡分配新 Worker
        """
        session_dir = f"{SANDBOX_ROOT}/sess_{conv_id}"

        # 1. 本地缓存命中 + Worker 健康
        if conv_id in self._sessions:
            worker_id, session_dir = self._sessions[conv_id]
            if worker_id in self._healthy:
                return self._all_workers[worker_id], session_dir
            logger.warning("Worker %s 不可用，session %s 故障转移", worker_id, conv_id)
            del self._sessions[conv_id]

        # 2. 从 DB 恢复（跨 worker / 刷新后）
        db_worker_id = await self._load_worker_id_from_db(conv_id)
        if db_worker_id and db_worker_id in self._healthy:
            self._sessions[conv_id] = (db_worker_id, session_dir)
            logger.info("Session 从 DB 恢复 | conv=%s → worker=%s", conv_id, db_worker_id)
            return self._all_workers[db_worker_id], session_dir

        # 3. 负载均衡分配
        if not self._healthy:
            raise RuntimeError("无健康的 Sandbox Worker")

        session_counts: dict[str, int] = {wid: 0 for wid in self._healthy}
        for _, (wid, _) in self._sessions.items():
            if wid in session_counts:
                session_counts[wid] += 1

        worker_id = min(session_counts, key=session_counts.get)  # type: ignore
        self._sessions[conv_id] = (worker_id, session_dir)

        # 写 DB 持久化
        await self._save_worker_id_to_db(conv_id, worker_id)

        logger.info(
            "Session 分配 | conv=%s → worker=%s | sessions=%d",
            conv_id, worker_id, session_counts[worker_id] + 1,
        )
        return self._all_workers[worker_id], session_dir

    @staticmethod
    async def _load_worker_id_from_db(conv_id: str) -> str:
        """从 DB 异步读取 sandbox_worker_id。"""
        try:
            from sqlalchemy import select
            from db.database import AsyncSessionLocal
            from db.models import ConversationModel
            async with AsyncSessionLocal() as session:
                row = await session.get(ConversationModel, conv_id)
                return getattr(row, "sandbox_worker_id", "") if row else ""
        except Exception:
            return ""

    @staticmethod
    async def _save_worker_id_to_db(conv_id: str, worker_id: str) -> None:
        """将 sandbox_worker_id 异步写入 DB。"""
        try:
            from sqlalchemy import update as sa_update
            from db.database import AsyncSessionLocal
            from db.models import ConversationModel
            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa_update(ConversationModel)
                    .where(ConversationModel.id == conv_id)
                    .values(sandbox_worker_id=worker_id)
                )
                await session.commit()
        except Exception as exc:
            logger.warning("sandbox_worker_id 写 DB 失败 | conv=%s | %s", conv_id, exc)

    # ══════════════════════════════════════════════════════════════════════════
    # 健康检查 + 自动恢复
    # ══════════════════════════════════════════════════════════════════════════

    async def _health_check_loop(self) -> None:
        """每 30s 检查所有 Worker 健康状态。"""
        while True:
            try:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                await self._health_check()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("健康检查异常: %s", exc)

    async def _health_check(self) -> None:
        for wid, worker in self._all_workers.items():
            try:
                result = await asyncio.wait_for(
                    worker.exec_command("echo ok", timeout=5),
                    timeout=10,
                )
                if result.exit_code == 0 and "ok" in result.stdout:
                    if wid not in self._healthy:
                        self._healthy.add(wid)
                        logger.info("Worker 恢复上线 | id=%s", wid)
                else:
                    raise RuntimeError(f"健康检查返回异常: exit={result.exit_code}")
            except Exception as exc:
                if wid in self._healthy:
                    self._healthy.discard(wid)
                    logger.warning("Worker 下线 | id=%s | error=%s", wid, exc)
                # 尝试重连
                try:
                    await worker.connect()
                    test = await worker.exec_command("echo ok", timeout=5)
                    if test.exit_code == 0:
                        self._healthy.add(wid)
                        logger.info("Worker 重连成功 | id=%s", wid)
                except Exception:
                    pass  # 重连失败，保持下线

    # ══════════════════════════════════════════════════════════════════════════
    # 核心操作（流式 + 同步两种模式）
    # ══════════════════════════════════════════════════════════════════════════

    _FILE_MAP = {
        "python":     ("main.py",    "python3 main.py"),
        "javascript": ("main.js",    "node main.js"),
        "java":       ("Main.java",  "javac Main.java && java -cp . Main"),
        "shell":      ("run.sh",     "bash run.sh"),
        "bash":       ("run.sh",     "bash run.sh"),
    }

    async def execute_code_streaming(
        self,
        conv_id: str,
        language: str,
        code: str,
        on_output=None,
        timeout: int | None = None,
    ) -> ExecuteResult:
        """
        流式执行代码：stdout/stderr 实时通过 on_output(stream, text) 回调推送。

        on_output: async callable(stream: str, text: str) → 每次有输出时调用
        """
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用：所有 Worker 均不健康",
                exit_code=-1, duration=0, cwd="", commands=[],
            )

        timeout = timeout or self._timeout
        worker, session_dir = await self._get_worker_for_session(conv_id)
        await worker.exec_command(f"mkdir -p {session_dir}")

        lang = language.lower().strip()
        if lang not in self._FILE_MAP:
            return ExecuteResult(
                stdout="", stderr=f"不支持的语言: {language}",
                exit_code=-1, duration=0, cwd=session_dir, commands=[],
            )

        filename, run_cmd = self._FILE_MAP[lang]
        filepath = f"{session_dir}/{filename}"
        commands: list[dict] = []

        # 写入代码文件
        await worker.write_file(filepath, code)
        line_count = code.count('\n') + 1
        commands.append({
            "cmd": f"cat > {filename} << 'EOF'  # ({line_count} lines, {len(code)} bytes)",
            "output": "", "exit_code": 0,
        })

        # 流式执行
        import json as _json
        import time as _time
        start = _time.monotonic()
        stdout_all: list[str] = []
        stderr_all: list[str] = []
        exit_code = 0

        async for stream, text in worker.exec_streaming(
            f"cd {session_dir} && {run_cmd}", timeout=timeout,
        ):
            if stream == "__done__":
                try:
                    info = _json.loads(text)
                    exit_code = info.get("exit_code", 0)
                except Exception:
                    pass
                continue
            if stream == "stdout":
                stdout_all.append(text)
            elif stream == "stderr":
                stderr_all.append(text)
            # 实时推送给前端
            if on_output:
                try:
                    await on_output(stream, text)
                except Exception:
                    pass

        duration = _time.monotonic() - start
        stdout_str = "".join(stdout_all)
        stderr_str = "".join(stderr_all)

        commands.append({
            "cmd": run_cmd,
            "output": stdout_str, "stderr": stderr_str,
            "exit_code": exit_code,
        })

        # 执行后 ls
        ls_result = await worker.exec_command(
            f"ls -la {session_dir} --color=never 2>/dev/null | tail -20"
        )
        if ls_result.stdout.strip():
            commands.append({"cmd": "ls -la", "output": ls_result.stdout, "exit_code": 0})

        logger.info(
            "代码执行完成（流式）| conv=%s | worker=%s | lang=%s | exit=%d | %.2fs",
            conv_id, worker.worker_id, language, exit_code, duration,
        )
        return ExecuteResult(
            stdout=stdout_str, stderr=stderr_str,
            exit_code=exit_code, duration=duration,
            cwd=session_dir, commands=commands,
        )

    async def run_shell_streaming(
        self,
        conv_id: str,
        command: str,
        on_output=None,
        timeout: int | None = None,
    ) -> ExecuteResult:
        """流式执行 shell 命令。"""
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用", exit_code=-1, duration=0,
                cwd="", commands=[],
            )

        timeout = timeout or self._timeout
        worker, session_dir = await self._get_worker_for_session(conv_id)
        await worker.exec_command(f"mkdir -p {session_dir}")

        import time as _time
        start = _time.monotonic()
        stdout_all: list[str] = []
        stderr_all: list[str] = []
        exit_code = 0

        async for stream, text in worker.exec_streaming(
            f"cd {session_dir} && {command}", timeout=timeout,
        ):
            if stream == "__done__":
                import json as _json2
                try:
                    info = _json2.loads(text)
                    exit_code = info.get("exit_code", 0)
                except Exception:
                    pass
                continue
            if stream == "stdout":
                stdout_all.append(text)
            elif stream == "stderr":
                stderr_all.append(text)
            if on_output:
                try:
                    await on_output(stream, text)
                except Exception:
                    pass

        duration = _time.monotonic() - start
        stdout_str = "".join(stdout_all)
        stderr_str = "".join(stderr_all)

        commands = [{
            "cmd": command,
            "output": stdout_str, "stderr": stderr_str,
            "exit_code": exit_code,
        }]

        logger.info(
            "Shell 流式执行完成 | conv=%s | worker=%s | cmd='%.80s' | exit=%d",
            conv_id, worker.worker_id, command, exit_code,
        )
        return ExecuteResult(
            stdout=stdout_str, stderr=stderr_str,
            exit_code=exit_code, duration=duration,
            cwd=session_dir, commands=commands,
        )

    # 保留同步版本给内部用（mkdir、ls 等快速命令）
    async def execute_code(
        self,
        conv_id: str,
        language: str,
        code: str,
        timeout: int | None = None,
    ) -> ExecuteResult:
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用：所有 Worker 均不健康",
                exit_code=-1, duration=0, cwd="", commands=[],
            )

        timeout = timeout or self._timeout
        worker, session_dir = await self._get_worker_for_session(conv_id)
        await worker.exec_command(f"mkdir -p {session_dir}")

        file_map = {
            "python":     ("main.py",    "python3 main.py"),
            "javascript": ("main.js",    "node main.js"),
            "java":       ("Main.java",  "javac Main.java && java -cp . Main"),
            "shell":      ("run.sh",     "bash run.sh"),
            "bash":       ("run.sh",     "bash run.sh"),
        }

        lang = language.lower().strip()
        if lang not in file_map:
            return ExecuteResult(
                stdout="",
                stderr=f"不支持的语言: {language}，支持: python, javascript, java, shell",
                exit_code=-1, duration=0, cwd=session_dir, commands=[],
            )

        filename, run_cmd = file_map[lang]
        filepath = f"{session_dir}/{filename}"
        commands: list[dict] = []

        await worker.write_file(filepath, code)
        line_count = code.count('\n') + 1
        commands.append({
            "cmd": f"cat > {filename} << 'EOF'  # ({line_count} lines, {len(code)} bytes)",
            "output": "", "exit_code": 0,
        })

        result = await worker.exec_command(f"cd {session_dir} && {run_cmd}", timeout=timeout)
        commands.append({
            "cmd": run_cmd,
            "output": result.stdout, "stderr": result.stderr,
            "exit_code": result.exit_code,
        })

        ls_result = await worker.exec_command(
            f"ls -la {session_dir} --color=never 2>/dev/null | tail -20"
        )
        if ls_result.stdout.strip():
            commands.append({"cmd": "ls -la", "output": ls_result.stdout, "exit_code": 0})

        logger.info(
            "代码执行完成 | conv=%s | worker=%s | lang=%s | exit=%d | %.2fs",
            conv_id, worker.worker_id, language, result.exit_code, result.duration,
        )
        return ExecuteResult(
            stdout=result.stdout, stderr=result.stderr,
            exit_code=result.exit_code, duration=result.duration,
            cwd=session_dir, commands=commands,
        )

    async def run_shell(
        self,
        conv_id: str,
        command: str,
        timeout: int | None = None,
    ) -> ExecuteResult:
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用", exit_code=-1, duration=0,
                cwd="", commands=[],
            )

        timeout = timeout or self._timeout
        worker, session_dir = await self._get_worker_for_session(conv_id)
        await worker.exec_command(f"mkdir -p {session_dir}")

        result = await worker.exec_command(f"cd {session_dir} && {command}", timeout=timeout)
        commands = [{
            "cmd": command,
            "output": result.stdout, "stderr": result.stderr,
            "exit_code": result.exit_code,
        }]

        logger.info(
            "Shell 执行完成 | conv=%s | worker=%s | cmd='%.80s' | exit=%d",
            conv_id, worker.worker_id, command, result.exit_code,
        )
        return ExecuteResult(
            stdout=result.stdout, stderr=result.stderr,
            exit_code=result.exit_code, duration=result.duration,
            cwd=session_dir, commands=commands,
        )

    async def write_file(self, conv_id: str, path: str, content: str) -> ExecuteResult:
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用", exit_code=-1, duration=0,
                cwd="", commands=[],
            )

        worker, session_dir = await self._get_worker_for_session(conv_id)
        await worker.exec_command(f"mkdir -p {session_dir}")
        full_path = f"{session_dir}/{path.lstrip('/')}"
        parent = "/".join(full_path.rsplit("/", 1)[:-1])
        if parent:
            await worker.exec_command(f"mkdir -p {parent}")
        await worker.write_file(full_path, content)

        stat_result = await worker.exec_command(
            f"ls -la {full_path} && echo '---' && wc -l < {full_path}"
        )

        line_count = content.count('\n') + 1
        commands = []
        if '/' in path:
            commands.append({
                "cmd": f"mkdir -p {'/'.join(path.split('/')[:-1])}",
                "output": "", "exit_code": 0,
            })
        commands.append({
            "cmd": f"cat > {path} << 'EOF'  # ({line_count} lines, {len(content)} bytes)",
            "output": "", "exit_code": 0,
        })
        commands.append({
            "cmd": f"ls -la {path}",
            "output": stat_result.stdout.split('---')[0].strip() if stat_result.stdout else "",
            "exit_code": 0,
        })

        return ExecuteResult(
            stdout=f"文件已写入: {path} ({line_count} 行, {len(content)} 字节)",
            stderr="", exit_code=0, duration=0,
            cwd=session_dir, commands=commands,
        )

    async def read_file(self, conv_id: str, path: str) -> ExecuteResult:
        if not self._healthy:
            return ExecuteResult(
                stdout="", stderr="沙箱不可用", exit_code=-1, duration=0,
                cwd="", commands=[],
            )

        worker, session_dir = await self._get_worker_for_session(conv_id)
        full_path = f"{session_dir}/{path.lstrip('/')}"
        result = await worker.exec_command(f"cat {full_path}")
        commands = [{
            "cmd": f"cat {path}",
            "output": result.stdout, "stderr": result.stderr,
            "exit_code": result.exit_code,
        }]
        return ExecuteResult(
            stdout=result.stdout, stderr=result.stderr,
            exit_code=result.exit_code, duration=result.duration,
            cwd=session_dir, commands=commands,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Session 清理
    # ══════════════════════════════════════════════════════════════════════════

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Session 清理异常: %s", exc)

    async def _cleanup_expired(self) -> None:
        cutoff_minutes = self._cleanup_hours * 60
        for wid in list(self._healthy):
            worker = self._all_workers[wid]
            try:
                result = await worker.exec_command(
                    f"find {SANDBOX_ROOT} -maxdepth 1 -name 'sess_*' -type d -mmin +{cutoff_minutes}"
                )
                if result.stdout.strip():
                    for d in result.stdout.strip().split('\n'):
                        d = d.strip()
                        if d and d.startswith(SANDBOX_ROOT):
                            await worker.exec_command(f"rm -rf {d}")
                            conv_id = d.rsplit("sess_", 1)[-1] if "sess_" in d else ""
                            self._sessions.pop(conv_id, None)
                            logger.info("已清理过期 session | worker=%s | dir=%s", wid, d)
            except Exception as exc:
                logger.warning("清理 Worker %s 失败: %s", wid, exc)


sandbox_manager = SandboxManager()
