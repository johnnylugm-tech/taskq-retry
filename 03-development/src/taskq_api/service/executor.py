"""Subprocess execution of a run.

[FR-02] Citations: SPEC.md:96-98.
[FR-08] Asynchronous executor: TaskGroup drain, concurrency cap, timeout kill, cancellation.
Citations: SPEC.md:145-150, SPEC.md:381.
"""
import asyncio
import os
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Optional

from taskq_api.repository.session import DbEngine
from taskq_api.repository.unit_of_work import UnitOfWork
from taskq_api.service import runner

TAIL_BYTES = 4096
_SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tail(data: bytes) -> str:
    text = data[-TAIL_BYTES:].decode(errors="replace")
    return _SECRET.sub("[REDACTED]", text)


def _set_status(engine: DbEngine, run_id: str, status: str, **fields: Any) -> None:
    with UnitOfWork(engine) as uow:
        row = uow.get_run(run_id)
        if row is None:
            raise RuntimeError(f"run {run_id} not found")
        if not runner.is_transition_allowed(row.status, status):
            raise RuntimeError(f"illegal run transition {row.status} -> {status}")
        row.status = status
        for k, v in fields.items():
            setattr(row, k, v)


async def execute(engine: DbEngine, run_id: str, command: str, timeout: Optional[float] = None) -> None:
    """Run `command` without a shell, enforcing the timeout (default TASKQ_TASK_TIMEOUT), and persist the outcome."""
    _set_status(engine, run_id, "running")
    if timeout is None:
        timeout = float(os.environ["TASKQ_TASK_TIMEOUT"])
    start = time.monotonic()
    out = err = b""
    exit_code = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        status, err = "failed", str(exc).encode()
    else:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            exit_code = proc.returncode
            status = "done" if exit_code == 0 else "failed"
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            status = "timeout"
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
    _set_status(
        engine, run_id, status, exit_code=exit_code, stdout_tail=_tail(out), stderr_tail=_tail(err),
        duration_ms=int((time.monotonic() - start) * 1000),
        finished_at=_utcnow())


class Executor:
    """Bounded background executor with graceful drain. [FR-08]

    Citations: SPEC.md:146-150, SPEC.md:381.
    """

    def __init__(self, engine: DbEngine, max_concurrent: int, drain_timeout: float, task_timeout: float) -> None:
        self._engine = engine
        self._drain_timeout = drain_timeout
        self._task_timeout = task_timeout
        self._sem = asyncio.Semaphore(max_concurrent)
        self._group: Optional[asyncio.TaskGroup] = None
        self._jobs: set[asyncio.Task[None]] = set()
        self.running = 0
        self.peak_running = 0

    async def start(self) -> None:
        """Open the TaskGroup that owns every submitted job."""
        self._group = asyncio.TaskGroup()
        await self._group.__aenter__()

    def submit(self, run_id: str, command: str) -> None:
        """Enqueue a run without blocking; it waits for a free concurrency slot."""
        assert self._group is not None, "Executor.start() must be called before submit()"
        job = self._group.create_task(self._job(run_id, command))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _job(self, run_id: str, command: str) -> None:
        try:
            await self.run(run_id, command)
        except asyncio.CancelledError:
            _set_status(self._engine, run_id, "interrupted", finished_at=_utcnow())
            raise
        except Exception:  # a failed run must not abort sibling jobs in the TaskGroup
            pass

    async def run(self, run_id: str, command: str) -> None:
        """Execute under the concurrency cap and wait for completion."""
        async with self._sem:
            self.running += 1
            self.peak_running = max(self.peak_running, self.running)
            try:
                await execute(self._engine, run_id, command, self._task_timeout)
            finally:
                self.running -= 1

    async def drain(self) -> None:
        """Wait up to drain_timeout for in-flight jobs; cancel (-> interrupted) the rest."""
        if self._group is None:
            return
        if self._jobs:
            _, pending = await asyncio.wait(set(self._jobs), timeout=self._drain_timeout)
            for job in pending:
                job.cancel()
        group, self._group = self._group, None
        await group.__aexit__(None, None, None)
