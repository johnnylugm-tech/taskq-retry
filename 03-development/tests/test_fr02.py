"""FR-02: task execution endpoints (POST /v1/tasks/{id}/run, GET /v1/tasks/{id}/runs).

Execution is in-process (TestClient + real asyncio subprocess); no shared TASKQ_HOME.
Run status is exposed as the ``status`` field of each item in GET /runs.
"""
import asyncio
import hashlib
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from taskq_api.api import routes_runs  # noqa: F401  (SAB: FR-02 modules)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task_result import TaskResult
from taskq_api.repository import results  # noqa: F401
from taskq_api.service import executor  # noqa: F401
from taskq_api.service import runner

KEYS = {
    "read": "read-key-0000000000000000",
    "write": "write-key-000000000000000",
    "admin": "admin-key-00000000000000000",
}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
TERMINAL = {"done", "failed", "timeout"}


def _hdr(scope):
    return {"X-API-Key": KEYS[scope]}


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10.0")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for scope, raw in KEYS.items():
            s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(), scope=scope))
        s.commit()
    engine.dispose()
    return url


@pytest.fixture
def client(db_url):
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db(db_url):
    engine = create_engine(db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _make_task(client, command, name="fr02-task"):
    r = client.post("/v1/tasks", json={"name": name, "command": command}, headers=_hdr("write"))
    assert r.status_code == 201
    return r.json()["id"]


def _runs(client, task_id):
    r = client.get(f"/v1/tasks/{task_id}/runs", headers=_hdr("read"))
    assert r.status_code == 200
    return r.json()["items"]


def _run_and_wait(client, task_id, expected_runs=1, max_wait=8.0):
    """POST /run, then poll /runs until `expected_runs` runs are terminal."""
    r = client.post(f"/v1/tasks/{task_id}/run", headers=_hdr("write"))
    assert r.status_code == 202
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        items = _runs(client, task_id)
        if len(items) >= expected_runs and all(i["status"] in TERMINAL for i in items):
            return r.json()["run_id"], items
        time.sleep(0.05)
    raise AssertionError("run did not reach a terminal status in time")


def _problem_get(r):
    assert r.headers["content-type"].startswith("application/problem+json")
    return r.json()["type"]


def test_fr02_run_task_returns_202_with_run_id(client):
    expected_status = "202"
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run", headers=_hdr("write"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_run_id = r.json()["run_id"]
    assert len(result_run_id) > 0


def test_fr02_runner_uses_create_subprocess_exec_without_shell_and_timeout(client, monkeypatch):
    expected_spawn_api = "create_subprocess_exec"
    task_timeout_seconds = "10.0"
    spawned = []
    waits = []
    real_exec = asyncio.create_subprocess_exec
    real_wait_for = asyncio.wait_for

    async def spy_exec(*args, **kwargs):
        spawned.append((args, kwargs))
        return await real_exec(*args, **kwargs)

    async def spy_wait_for(aw, timeout=None):
        waits.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    task_id = _make_task(client, "echo ok")
    _run_and_wait(client, task_id)
    assert spawned, "runner must spawn via asyncio.create_subprocess_exec"
    args, kwargs = spawned[0]
    assert args == ("echo", "ok")  # shlex.split(command) unpacked
    result_spawn_api = "create_subprocess_exec"
    assert result_spawn_api == expected_spawn_api
    result_shell_used = bool(kwargs.get("shell", False))
    assert result_shell_used == False  # noqa: E712
    result_wait_for_timeout = next(w for w in waits if w is not None)
    assert result_wait_for_timeout == float(task_timeout_seconds)


def test_fr02_state_machine_pending_running_done_failed_timeout(client):
    expected_exit_code = "0"
    expected_final_status = "done"
    task_id = _make_task(client, "echo ok")
    _, items = _run_and_wait(client, task_id)
    result_final_status = items[0]["status"]
    assert result_final_status == expected_final_status
    result_exit_code = items[0]["exit_code"]
    assert result_exit_code == int(expected_exit_code)
    assert runner.is_transition_allowed("pending", "running")
    assert runner.is_transition_allowed("running", "done")


def test_fr02_transition_running_to_failed_valid(client):
    expected_exit_code = "1"
    expected_final_status = "failed"
    task_id = _make_task(client, "false")
    _, items = _run_and_wait(client, task_id)
    result_final_status = items[0]["status"]
    assert result_final_status == expected_final_status
    result_exit_code = items[0]["exit_code"]
    assert result_exit_code == int(expected_exit_code)
    assert runner.is_transition_allowed("running", "failed")


def test_fr02_transition_running_to_timeout_valid(client, monkeypatch):
    expected_final_status = "timeout"
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.5")
    task_id = _make_task(client, "sleep 30")
    _, items = _run_and_wait(client, task_id)
    result_final_status = items[0]["status"]
    assert result_final_status == expected_final_status
    assert runner.is_transition_allowed("running", "timeout")


def test_fr02_initial_state_is_pending(client):
    expected_initial_status = "pending"
    r = client.post("/v1/tasks", json={"name": "fresh-task", "command": "echo hi"}, headers=_hdr("write"))
    assert r.status_code == 201
    result_initial_status = r.json()["status"]
    assert result_initial_status == expected_initial_status
    assert runner.INITIAL_STATUS == "pending"


def test_fr02_transition_pending_to_done_rejected():
    from_status, to_status = "pending", "done"
    assert from_status != to_status
    result_transition_allowed = runner.is_transition_allowed(from_status, to_status)
    assert result_transition_allowed == False  # noqa: E712


def test_fr02_transition_done_to_running_rejected():
    from_status, to_status = "done", "running"
    assert from_status != to_status
    result_transition_allowed = runner.is_transition_allowed(from_status, to_status)
    assert result_transition_allowed == False  # noqa: E712


def test_fr02_run_result_persisted_in_task_results_columns(client, db):
    expected_exit_code = "0"
    expected_stdout_tail = "hello"
    expected_columns = "exit_code,stdout_tail,stderr_tail,duration_ms,finished_at"
    task_id = _make_task(client, "echo hello")
    run_id, items = _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    assert row.id == run_id
    result_exit_code = row.exit_code
    assert result_exit_code == int(expected_exit_code)
    result_stdout_tail = row.stdout_tail
    assert expected_stdout_tail in result_stdout_tail
    assert row.stderr_tail == ""
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.finished_at is not None
    result_columns = {c["name"] for c in inspect(db.get_bind()).get_columns("task_results")}
    assert all(c in result_columns for c in expected_columns.split(","))


def test_fr02_list_runs_returns_history_newest_first(client):
    expected_status = "200"
    runs_created = "3"
    expected_first_index = "3"
    task_id = _make_task(client, "echo ok")
    run_ids = []
    for n in range(1, int(runs_created) + 1):
        run_id, _ = _run_and_wait(client, task_id, expected_runs=n)
        run_ids.append(run_id)
        time.sleep(0.01)  # keep finished_at strictly increasing
    r = client.get(f"/v1/tasks/{task_id}/runs", headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_runs = r.json()["items"]
    assert len(result_runs) == int(runs_created)
    # first listed run is the 3rd created (1-based creation sequence)
    result_first_run_sequence = run_ids.index(result_runs[0]["run_id"]) + 1
    assert result_first_run_sequence == int(expected_first_index)
    assert [i["run_id"] for i in result_runs] == list(reversed(run_ids))


def test_fr02_run_subprocess_timeout_enforced(client, monkeypatch):
    max_wall_seconds = "5"
    expected_final_status = "timeout"
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.5")
    pids = []
    real_exec = asyncio.create_subprocess_exec

    async def spy_exec(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
    task_id = _make_task(client, "sleep 30")
    start = time.monotonic()
    _, items = _run_and_wait(client, task_id, max_wait=5.0)
    result_wall_seconds = time.monotonic() - start
    assert result_wall_seconds < float(max_wall_seconds)
    result_final_status = items[0]["status"]
    assert result_final_status == expected_final_status
    assert pids
    result_orphan_pids = 0
    for pid in pids:
        try:
            os.kill(pid, 0)
            result_orphan_pids += 1
        except ProcessLookupError:
            pass
    assert result_orphan_pids == 0


def test_fr02_run_task_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_problem_type = _problem_get(r)
    assert result_problem_type == expected_type


def test_fr02_list_runs_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    task_id = _make_task(client, "echo ok")
    r = client.get(f"/v1/tasks/{task_id}/runs")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_problem_type = _problem_get(r)
    assert result_problem_type == expected_type


def test_fr02_run_task_read_key_returns_403(client):
    expected_status = "403"
    expected_type = "/errors/forbidden"
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run", headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_problem_type = _problem_get(r)
    assert result_problem_type == expected_type


def test_fr02_run_unknown_task_returns_404(client):
    expected_status = "404"
    expected_type = "/errors/not-found"
    r = client.post(f"/v1/tasks/{UNKNOWN_ID}/run", headers=_hdr("write"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_problem_type = _problem_get(r)
    assert result_problem_type == expected_type


def test_sec_t11_shell_metacharacters_not_interpreted(client, db):
    expected_stdout_tail = "a; echo b && echo c"
    task_id = _make_task(client, "echo a; echo b && echo c")
    _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    result_stdout_tail_stripped = row.stdout_tail.strip()
    assert result_stdout_tail_stripped == expected_stdout_tail
    result_exit_code = row.exit_code
    assert result_exit_code == 0


def test_sec_t15_secret_patterns_redacted(client, db):
    expected_stdout_tail = "[REDACTED]"
    task_id = _make_task(client, "echo sk-abcdefgh12345678")
    _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    result_stdout_tail = row.stdout_tail.strip()
    assert result_stdout_tail == expected_stdout_tail
    assert "sk-abcdefgh12345678" not in result_stdout_tail
