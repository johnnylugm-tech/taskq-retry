"""FR-08: asynchronous executor (TaskGroup drain, concurrency cap, timeout kill, cancellation).

Execution is in-process (real asyncio subprocess, tmp sqlite DB); no shared TASKQ_HOME.
GREEN TODO: ``executor.Executor(engine, max_concurrent, drain_timeout, task_timeout)`` with
``async start()``, ``submit(run_id, command)`` (non-blocking enqueue), ``async run(run_id, command)``
(semaphore-gated, awaits completion), ``async drain()``, and attributes ``peak_running`` / ``running``.
``runner`` must allow ``running -> interrupted``; ``create_app()`` must expose ``app.state.executor``.
"""
import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.models.task_result import TaskResult
from taskq_api.service import executor, runner  # noqa: F401


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def spawned(monkeypatch):
    """Record every subprocess the executor spawns so orphans can be checked."""
    procs = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    return procs


def _seed(engine, command):
    run_id = str(uuid.uuid4())
    with Session(engine) as s:
        task = Task(command=command, name=f"t-{run_id}")
        s.add(task)
        s.flush()
        s.add(TaskResult(id=run_id, task_id=task.id, status="pending"))
        s.commit()
    return run_id


def _status(engine, run_id):
    with Session(engine) as s:
        return s.get(TaskResult, run_id).status


def _row(engine, run_id):
    with Session(engine) as s:
        r = s.get(TaskResult, run_id)
        return r.status, r.exit_code


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _orphans(procs):
    return sum(1 for p in procs if _alive(p.pid))


def test_fr08_graceful_drain_waits_then_marks_interrupted(engine, spawned):
    # AC8.1-final-status, AC8.1-drain-bounded, AC8.1-runtime-over-drain, AC8.1-no-orphan-after-drain
    drain_timeout = "1.0"
    task_runtime_seconds = "5"
    expected_final_status = "interrupted"
    run_id = _seed(engine, "sleep 5")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=1.0, task_timeout=30.0)
        await ex.start()
        ex.submit(run_id, "sleep 5")
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        await ex.drain()
        return time.monotonic() - t0

    result_drain_seconds = asyncio.run(scenario())
    result_final_status = _status(engine, run_id)
    result_orphan_pids = _orphans(spawned)
    assert result_final_status == expected_final_status  # AC8.1-final-status
    assert result_drain_seconds <= float(drain_timeout) + 1.0  # AC8.1-drain-bounded
    assert float(task_runtime_seconds) > float(drain_timeout)  # AC8.1-runtime-over-drain
    assert result_drain_seconds >= 0.5
    assert result_orphan_pids == 0  # AC8.1-no-orphan-after-drain


def test_fr08_drain_waits_for_in_flight_task_to_finish(engine):
    # AC8.1-final-status, AC8.1-drain-bounded, AC8.1-runtime-within-drain
    drain_timeout = "5.0"
    task_runtime_seconds = "0.5"
    expected_final_status = "done"
    run_id = _seed(engine, "sleep 0.5")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=30.0)
        await ex.start()
        ex.submit(run_id, "sleep 0.5")
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await ex.drain()
        return time.monotonic() - t0

    result_drain_seconds = asyncio.run(scenario())
    result_final_status = _status(engine, run_id)
    assert result_final_status == expected_final_status  # AC8.1-final-status
    assert result_drain_seconds <= float(drain_timeout) + 1.0  # AC8.1-drain-bounded
    assert float(task_runtime_seconds) < float(drain_timeout)  # AC8.1-runtime-within-drain


def test_fr08_drain_with_no_inflight_tasks_returns_immediately(engine, monkeypatch):
    # AC8.1-empty-drain, AC8.1-empty-inflight
    max_wall_seconds = "1"
    inflight_tasks = "0"

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=30.0, task_timeout=30.0)
        await ex.start()
        assert ex.running == float(inflight_tasks)  # AC8.1-empty-inflight
        t0 = time.monotonic()
        await ex.drain()
        return time.monotonic() - t0

    result_drain_seconds = asyncio.run(scenario())
    assert result_drain_seconds < float(max_wall_seconds)  # AC8.1-empty-drain
    assert float(inflight_tasks) == 0  # AC8.1-empty-inflight

    # the app lifespan owns an executor and drains it on shutdown
    monkeypatch.setenv("TASKQ_DRAIN_TIMEOUT", "30.0")
    monkeypatch.setenv("TASKQ_MAX_CONCURRENT", "2")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10.0")
    t0 = time.monotonic()
    with TestClient(create_app()) as client:
        assert client.app.state.executor.running == 0
        assert client.app.state.executor._max_concurrent == 2  # TASKQ_MAX_CONCURRENT is honoured
        assert client.app.state.executor._drain_timeout == 30.0  # TASKQ_DRAIN_TIMEOUT is honoured
    assert time.monotonic() - t0 < 5


def test_fr08_timeout_kills_process_and_awaits_wait_no_orphans(engine, spawned):
    # AC8.1-final-status, AC8.3-no-orphan, AC8.3-killed, AC8.3-wait-awaited
    expected_final_status = "timeout"
    run_id = _seed(engine, "sleep 30")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=0.5)
        await ex.start()
        await ex.run(run_id, "sleep 30")
        await ex.drain()

    asyncio.run(scenario())
    result_final_status = _status(engine, run_id)
    assert len(spawned) == 1
    result_orphan_pids = _orphans(spawned)
    result_returncode = spawned[0].returncode
    # returncode is only populated once proc.wait() has completed
    result_wait_awaited = result_returncode is not None
    assert result_final_status == expected_final_status  # AC8.1-final-status
    assert result_orphan_pids == 0  # AC8.3-no-orphan
    assert result_returncode < 0  # AC8.3-killed
    assert result_wait_awaited == True  # AC8.3-wait-awaited  # noqa: E712


def test_fr08_concurrency_cap_queues_excess_tasks(engine):
    # AC8.2-peak, AC8.2-all-complete, AC8.2-oversubscribed
    max_concurrent = "2"
    tasks_submitted = "5"
    ids = [_seed(engine, "sleep 0.5") for _ in range(int(tasks_submitted))]

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=10.0, task_timeout=10.0)
        await ex.start()
        await asyncio.gather(*(ex.run(i, "sleep 0.5") for i in ids))
        await ex.drain()
        return ex.peak_running

    result_peak_running = asyncio.run(scenario())
    result_completed_count = sum(1 for i in ids if _status(engine, i) == "done")
    assert result_peak_running <= int(max_concurrent)  # AC8.2-peak
    assert result_completed_count == int(tasks_submitted)  # AC8.2-all-complete
    assert float(tasks_submitted) > int(max_concurrent)  # AC8.2-oversubscribed


def test_fr08_submit_state_transition_under_concurrent_load(engine):
    # NP13-peak-exact, NP13-final-running, NP13-completed (one executor shared by 20 callers)
    max_concurrent = "3"
    concurrent_callers = "20"
    ids = [_seed(engine, "sleep 0.1") for _ in range(int(concurrent_callers))]

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=3, drain_timeout=10.0, task_timeout=10.0)
        await ex.start()
        await asyncio.gather(*(ex.run(i, "sleep 0.1") for i in ids))
        peak, final_running = ex.peak_running, ex.running
        await ex.drain()
        return peak, final_running

    result_peak_running, result_final_running = asyncio.run(scenario())
    result_completed_count = sum(1 for i in ids if _status(engine, i) == "done")
    assert result_peak_running == int(max_concurrent)  # NP13-peak-exact
    assert result_final_running == 0  # NP13-final-running
    assert result_completed_count == int(concurrent_callers)  # NP13-completed


def test_fr08_cancelled_error_propagates_not_swallowed(engine, spawned):
    # AC8.4-type, AC8.4-not-swallowed: cancel a real running subprocess through the real handlers
    expected_exception_type = "CancelledError"
    run_id = _seed(engine, "sleep 30")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=60.0)
        await ex.start()
        job = asyncio.ensure_future(ex.run(run_id, "sleep 30"))
        await asyncio.sleep(0.5)
        job.cancel()
        raised = None
        try:
            await job
        except asyncio.CancelledError as exc:
            raised = type(exc).__name__
        running_after = ex.running
        await ex.drain()
        return raised, running_after

    result_exception_type, running_after = asyncio.run(scenario())
    result_swallowed = result_exception_type is None
    assert result_exception_type == expected_exception_type  # AC8.4-type
    assert result_swallowed == False  # AC8.4-not-swallowed  # noqa: E712
    assert running_after == 0
    assert len(spawned) == 1
    assert _orphans(spawned) == 0
    assert spawned[0].returncode is not None and spawned[0].returncode < 0


def test_fr08_command_not_found_marks_failed(engine):
    # AC8.1-final-status, AC8.3-failed-run
    expected_final_status = "failed"
    run_id = _seed(engine, "nonexistent-binary-xyz")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=10.0)
        await ex.start()
        await ex.run(run_id, "nonexistent-binary-xyz")
        await ex.drain()

    asyncio.run(scenario())
    result_final_status, result_exit_code = _row(engine, run_id)
    assert result_final_status == expected_final_status  # AC8.1-final-status
    assert result_exit_code != 0  # AC8.3-failed-run


def test_sec_t12_timeout_kills_subprocess_no_orphan(engine, spawned):
    # AC8.1-final-status, AC8.3-no-orphan, AC8.3-killed, AC8.3-wait-awaited
    expected_final_status = "timeout"
    run_id = _seed(engine, "sleep 60")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=0.5)
        await ex.start()
        await ex.run(run_id, "sleep 60")
        await ex.drain()

    asyncio.run(scenario())
    result_final_status = _status(engine, run_id)
    result_orphan_pids = _orphans(spawned)
    result_returncode = spawned[0].returncode
    result_wait_awaited = result_returncode is not None
    assert result_final_status == expected_final_status  # AC8.1-final-status
    assert result_orphan_pids == 0  # AC8.3-no-orphan
    assert result_returncode < 0  # AC8.3-killed
    assert result_wait_awaited == True  # AC8.3-wait-awaited  # noqa: E712


def test_sec_t13_concurrency_cap_enforced(engine):
    # AC8.2-peak, AC8.2-all-complete, AC8.2-oversubscribed
    max_concurrent = "2"
    tasks_submitted = "10"
    ids = [_seed(engine, "sleep 0.2") for _ in range(int(tasks_submitted))]

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=10.0, task_timeout=10.0)
        await ex.start()
        await asyncio.gather(*(ex.run(i, "sleep 0.2") for i in ids))
        await ex.drain()
        return ex.peak_running

    result_peak_running = asyncio.run(scenario())
    result_completed_count = sum(1 for i in ids if _status(engine, i) == "done")
    assert result_peak_running <= int(max_concurrent)  # AC8.2-peak
    assert result_completed_count == int(tasks_submitted)  # AC8.2-all-complete
    assert float(tasks_submitted) > int(max_concurrent)  # AC8.2-oversubscribed


def test_fr08_set_status_unknown_run_raises(engine):
    with pytest.raises(RuntimeError, match="not found"):
        executor._set_status(engine, "no-such-run", "running")


def test_fr08_set_status_illegal_transition_raises(engine):
    run_id = _seed(engine, "echo hi")
    with pytest.raises(RuntimeError, match="illegal run transition"):
        executor._set_status(engine, run_id, "done")
    assert _status(engine, run_id) == "pending"


def test_fr08_failed_job_does_not_abort_sibling_jobs(engine):
    good = _seed(engine, "echo ok")

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=5.0, task_timeout=10.0)
        await ex.start()
        ex.submit("no-such-run", "echo bad")  # execute() raises RuntimeError inside the job
        ex.submit(good, "echo ok")
        await ex.drain()

    asyncio.run(scenario())
    assert _status(engine, good) == "done"


def test_fr08_drain_before_start_is_noop(engine):
    async def scenario():
        ex = executor.Executor(engine, max_concurrent=2, drain_timeout=1.0, task_timeout=10.0)
        await ex.drain()

    asyncio.run(scenario())


def test_fr08_execute_defaults_timeout_from_env(engine, monkeypatch):
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    run_id = _seed(engine, "echo hi")
    asyncio.run(executor.execute(engine, run_id, "echo hi"))
    assert _status(engine, run_id) == "done"
