"""
SSHWorker：通过 SSH 连接远程沙箱执行命令

流式执行核心设计：
  用两个并发 task 分别读 stdout 和 stderr 放入 asyncio.Queue，
  主循环从 queue 中 yield，实现真正的逐块流式输出。
  两个 reader 都 EOF 后放入 sentinel 信号，主循环退出。
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator

logger = logging.getLogger("sandbox.worker")

# Queue sentinel：两个 reader 都结束后放入，主循环收到即退出
_SENTINEL = object()


@dataclass
class ExecuteResult:
    """命令执行结果 + 终端渲染上下文。"""
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    cwd: str = ""
    commands: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def is_timeout(self) -> bool:
        return self.exit_code == -1 and "超时" in self.stderr

    def to_display(self, max_len: int = 4000) -> str:
        prompt = f"root@sandbox:{self.cwd}$" if self.cwd else "$"
        parts: list[str] = []
        if self.commands:
            for c in self.commands:
                cmd = c.get("cmd", "")
                out = c.get("output", "")
                err = c.get("stderr", "")
                code = c.get("exit_code", 0)
                parts.append(f"{prompt} {cmd}")
                if out:
                    truncated = out[:max_len]
                    if len(out) > max_len:
                        truncated += f"\n... (截断，共 {len(out)} 字符)"
                    parts.append(truncated)
                if err:
                    parts.append(f"[stderr] {err[:1000]}")
                if code != 0:
                    parts.append(f"[exit_code={code}]")
        else:
            parts.append(f"{prompt} (command)")
            if self.stdout:
                parts.append(self.stdout[:max_len])
            if self.stderr:
                parts.append(f"[stderr] {self.stderr[:1000]}")
        if self.duration > 0:
            parts.append(f"\n⏱ {self.duration:.2f}s | exit={self.exit_code}")

        # 超时时追加恢复建议，告诉模型怎么处理
        if self.is_timeout:
            parts.append(
                "\n💡 恢复建议：命令执行超时，进程已被终止。"
                "如果是编译/构建类长命令（mvn、gradle、npm install 等），请改用后台执行：\n"
                "  nohup <命令> > /tmp/build.log 2>&1 &\n"
                "  sleep 3 && tail -50 /tmp/build.log\n"
                "这样可以避免超时，并通过 tail 查看进度。"
            )

        return "\n".join(parts)


class SSHWorker:

    def __init__(self, worker_id: str, host: str, port: int = 22,
                 user: str = "root", key_file: str = "", password: str = "",
                 pool_size: int = 3) -> None:
        self.worker_id = worker_id
        self._host = host
        self._port = port
        self._user = user
        self._key_file = key_file
        self._password = password
        self._pool_size = pool_size
        # SSH 连接池：多个连接轮流使用，避免单连接上 channel 过多
        self._pool: list = []
        self._pool_index = 0
        self._pool_lock = asyncio.Lock()
        self._pool_last_used: dict[int, float] = {}  # idx → 最后使用时间戳

    def _ssh_kwargs(self) -> dict:
        kwargs: dict = {
            "host": self._host, "port": self._port,
            "username": self._user, "known_hosts": None,
        }
        if self._key_file:
            kwargs["client_keys"] = [self._key_file]
        if self._password:
            kwargs["password"] = self._password
        return kwargs

    async def connect(self) -> None:
        """建立连接池（pool_size 个 SSH 连接）。"""
        import asyncssh
        kwargs = self._ssh_kwargs()
        self._pool = []
        for i in range(self._pool_size):
            conn = await asyncssh.connect(**kwargs)
            self._pool.append(conn)
        logger.info(
            "SSH 连接池建立 | worker=%s | host=%s:%d | pool=%d",
            self.worker_id, self._host, self._port, len(self._pool),
        )

    _HEALTH_CHECK_INTERVAL = 30  # 只对闲置超过 30 秒的连接做探活

    async def _get_conn(self):
        """轮询获取一个健康连接（Round-Robin + 按需探活）。"""
        import asyncssh
        now = time.monotonic()
        async with self._pool_lock:
            for _ in range(self._pool_size):
                idx = self._pool_index % len(self._pool)
                self._pool_index += 1
                conn = self._pool[idx]
                if conn.is_closed():
                    try:
                        conn = await asyncssh.connect(**self._ssh_kwargs())
                        self._pool[idx] = conn
                        self._pool_last_used[idx] = now
                        return conn
                    except Exception:
                        continue
                # 最近使用过的连接直接返回（跳过探活，减少延迟）
                if now - self._pool_last_used.get(idx, 0) < self._HEALTH_CHECK_INTERVAL:
                    self._pool_last_used[idx] = now
                    return conn
                # 闲置超过 30 秒，发探活命令确认
                try:
                    result = await asyncio.wait_for(
                        conn.run("echo ok", check=False, timeout=3), timeout=5,
                    )
                    if result.exit_status == 0:
                        self._pool_last_used[idx] = now
                        return conn
                except Exception:
                    pass
                # 探活失败，重建连接
                try:
                    conn = await asyncssh.connect(**self._ssh_kwargs())
                    self._pool[idx] = conn
                    self._pool_last_used[idx] = now
                    return conn
                except Exception:
                    continue
            # 所有连接都不可用，尝试重建第一个
            new_conn = await asyncssh.connect(**self._ssh_kwargs())
            self._pool[0] = new_conn
            self._pool_last_used[0] = now
            return new_conn

    async def close(self) -> None:
        for conn in self._pool:
            if not conn.is_closed():
                conn.close()
                await conn.wait_closed()
        self._pool = []

    async def exec_command(self, cmd: str, timeout: int = 30) -> ExecuteResult:
        """同步执行，等待完成。"""
        conn = await self._get_conn()
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                conn.run(cmd, check=False, timeout=timeout),
                timeout=timeout + 5,
            )
            return ExecuteResult(
                stdout=result.stdout or "", stderr=result.stderr or "",
                exit_code=result.exit_status or 0, duration=time.monotonic() - start,
            )
        except asyncio.TimeoutError:
            return ExecuteResult(stdout="", stderr=f"执行超时（{timeout}秒限制）",
                                exit_code=-1, duration=time.monotonic() - start)
        except Exception as exc:
            return ExecuteResult(stdout="", stderr=f"执行失败: {exc}",
                                exit_code=-1, duration=time.monotonic() - start)

    async def exec_streaming(
        self, cmd: str, timeout: int = 30,
    ) -> AsyncGenerator[tuple[str, str], None]:
        """
        流式执行命令。

        架构：
          stdout_reader task → queue.put(("stdout", chunk))
          stderr_reader task → queue.put(("stderr", chunk))
          两个都 EOF 后 → queue.put(_SENTINEL)
          主循环从 queue.get() 逐个 yield → 前端实时看到输出

        yield ("stdout"|"stderr", text) 或 ("__done__", json)
        """
        conn = await self._get_conn()
        import asyncssh

        start = time.monotonic()
        queue: asyncio.Queue = asyncio.Queue()

        try:
            async with conn.create_process(cmd, stderr=asyncssh.PIPE) as proc:

                async def _reader(stream, name: str) -> None:
                    """持续读取直到 EOF，每块数据放入队列。"""
                    try:
                        while True:
                            data = await stream.read(4096)
                            if not data:
                                break
                            await queue.put((name, data))
                    except Exception as exc:
                        await queue.put(("stderr", f"[{name} error: {exc}]\n"))

                async def _watchdog() -> None:
                    """超时后 kill 进程。"""
                    await asyncio.sleep(timeout)
                    if not proc.is_closing():
                        proc.kill()
                        await queue.put(("stderr", f"\n⚠️ 执行超时（{timeout}秒限制）\n"))

                stdout_task = asyncio.create_task(_reader(proc.stdout, "stdout"))
                stderr_task = asyncio.create_task(_reader(proc.stderr, "stderr"))
                watchdog_task = asyncio.create_task(_watchdog())

                # 等 reader 们结束后放 sentinel（在后台执行）
                async def _wait_readers():
                    await asyncio.gather(stdout_task, stderr_task)
                    await queue.put(_SENTINEL)
                waiter_task = asyncio.create_task(_wait_readers())

                # ── 主循环：从 queue 实时 yield ──
                while True:
                    item = await queue.get()
                    if item is _SENTINEL:
                        break
                    yield item

                watchdog_task.cancel()
                waiter_task.cancel()

                # 等进程退出码
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
                exit_code = proc.exit_status if proc.exit_status is not None else -1

        except Exception as exc:
            yield ("stderr", f"\n执行异常: {exc}\n")
            exit_code = -1

        duration = time.monotonic() - start
        yield ("__done__", f'{{"exit_code":{exit_code},"duration":{duration:.2f}}}')

    async def write_file(self, remote_path: str, content: str) -> None:
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        await self.exec_command(f"echo '{encoded}' | base64 -d > {remote_path}", timeout=10)
