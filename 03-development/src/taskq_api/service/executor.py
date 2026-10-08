"""Subprocess execution of a run.

[FR-02] Citations: SPEC.md:96-98.
"""
import asyncio
import os
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any

from taskq_api.repository.session import DbEngine
from taskq_api.repository.unit_of_work import UnitOfWork
from taskq_api.service import runner

TAIL_BYTES = 4096
_SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}")


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


async def execute(engine: DbEngine, run_id: str, command: str) -> None:
    """Run `command` without a shell, enforcing TASKQ_TASK_TIMEOUT, and persist the outcome."""
    _set_status(engine, run_id, "running")
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
    _set_status(
        engine, run_id, status, exit_code=exit_code, stdout_tail=_tail(out), stderr_tail=_tail(err),
        duration_ms=int((time.monotonic() - start) * 1000),
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
