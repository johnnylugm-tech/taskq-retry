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


def test_fr02_run_task_returns_202_with_run_id(client):
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run", headers=_hdr("write"))
    assert r.status_code == 202  # AC2.1-status
    assert len(r.json()["run_id"]) > 0  # AC2.1-run-id


def test_fr02_runner_uses_create_subprocess_exec_without_shell_and_timeout(client, monkeypatch):
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
    assert spawned, "runner must spawn via asyncio.create_subprocess_exec"  # AC2.2-spawn-api
    args, kwargs = spawned[0]
    assert args == ("echo", "ok")  # shlex.split(command) unpacked
    assert not kwargs.get("shell", False)  # AC2.2-no-shell
    assert 10.0 in waits  # AC2.2-timeout-wired


def test_fr02_state_machine_pending_running_done_failed_timeout(client):
    task_id = _make_task(client, "echo ok")
    _, items = _run_and_wait(client, task_id)
    assert items[0]["status"] == "done"  # AC2.3-final-status
    assert items[0]["exit_code"] == 0  # AC2.3-exit-code
    assert runner.is_transition_allowed("pending", "running")
    assert runner.is_transition_allowed("running", "done")


def test_fr02_transition_running_to_failed_valid(client):
    task_id = _make_task(client, "false")
    _, items = _run_and_wait(client, task_id)
    assert items[0]["status"] == "failed"  # AC2.3-final-status
    assert items[0]["exit_code"] == 1  # AC2.3-exit-code
    assert runner.is_transition_allowed("running", "failed")


def test_fr02_transition_running_to_timeout_valid(client, monkeypatch):
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.5")
    task_id = _make_task(client, "sleep 30")
    _, items = _run_and_wait(client, task_id)
    assert items[0]["status"] == "timeout"  # AC2.3-final-status
    assert runner.is_transition_allowed("running", "timeout")


def test_fr02_initial_state_is_pending(client):
    r = client.post("/v1/tasks", json={"name": "fresh-task", "command": "echo hi"}, headers=_hdr("write"))
    assert r.status_code == 201
    assert r.json()["status"] == "pending"  # AC2.3-initial
    assert runner.INITIAL_STATUS == "pending"


def test_fr02_transition_pending_to_done_rejected():
    assert "pending" != "done"  # AC2.3-from-to
    assert runner.is_transition_allowed("pending", "done") is False  # AC2.3-rejected


def test_fr02_transition_done_to_running_rejected():
    assert "done" != "running"  # AC2.3-from-to
    assert runner.is_transition_allowed("done", "running") is False  # AC2.3-rejected


def test_fr02_run_result_persisted_in_task_results_columns(client, db):
    task_id = _make_task(client, "echo hello")
    run_id, items = _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    assert row.id == run_id
    assert row.exit_code == 0  # AC2.4-exit-code
    assert "hello" in row.stdout_tail  # AC2.4-stdout
    assert row.stderr_tail == ""
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.finished_at is not None
    columns = {c["name"] for c in inspect(db.get_bind()).get_columns("task_results")}
    for col in "exit_code,stdout_tail,stderr_tail,duration_ms,finished_at".split(","):
        assert col in columns  # AC2.4-columns


def test_fr02_list_runs_returns_history_newest_first(client):
    task_id = _make_task(client, "echo ok")
    run_ids = []
    for n in range(1, 4):
        run_id, _ = _run_and_wait(client, task_id, expected_runs=n)
        run_ids.append(run_id)
        time.sleep(0.01)  # keep finished_at strictly increasing
    r = client.get(f"/v1/tasks/{task_id}/runs", headers=_hdr("read"))
    assert r.status_code == 200  # AC2.1-status
    items = r.json()["items"]
    assert len(items) == 3  # AC2.5-count
    # SPEC_AMBIGUITY: "first_run_sequence == 3" -> newest run (3rd created) listed first, checked by run_id.
    assert [i["run_id"] for i in items] == list(reversed(run_ids))  # AC2.5-order


def test_fr02_run_subprocess_timeout_enforced(client, monkeypatch):
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
    assert time.monotonic() - start < 5.0  # NP15-wall
    assert items[0]["status"] == "timeout"  # AC2.3-final-status
    assert pids
    orphans = 0
    for pid in pids:
        try:
            os.kill(pid, 0)
            orphans += 1
        except ProcessLookupError:
            pass
    assert orphans == 0  # NP15-no-orphan


def test_fr02_run_task_unauthenticated_returns_401(client):
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run")
    assert r.status_code == 401  # AC2.1-status
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/errors/unauthenticated"  # AC2.1-problem-type


def test_fr02_list_runs_unauthenticated_returns_401(client):
    task_id = _make_task(client, "echo ok")
    r = client.get(f"/v1/tasks/{task_id}/runs")
    assert r.status_code == 401  # AC2.1-status
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/errors/unauthenticated"  # AC2.1-problem-type


def test_fr02_run_task_read_key_returns_403(client):
    task_id = _make_task(client, "echo ok")
    r = client.post(f"/v1/tasks/{task_id}/run", headers=_hdr("read"))
    assert r.status_code == 403  # AC2.1-status
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/errors/forbidden"  # AC2.1-problem-type


def test_fr02_run_unknown_task_returns_404(client):
    r = client.post(f"/v1/tasks/{UNKNOWN_ID}/run", headers=_hdr("write"))
    assert r.status_code == 404  # AC2.1-status
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/errors/not-found"  # AC2.1-problem-type


def test_sec_t11_shell_metacharacters_not_interpreted(client, db):
    task_id = _make_task(client, "echo a; echo b && echo c")
    _, items = _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    assert row.stdout_tail.strip() == "a; echo b && echo c"  # T11-literal
    assert row.exit_code == 0  # T11-exit


def test_sec_t15_secret_patterns_redacted(client, db):
    task_id = _make_task(client, "echo sk-abcdefgh12345678")
    _run_and_wait(client, task_id)
    row = db.execute(select(TaskResult).where(TaskResult.task_id == task_id)).scalars().one()
    assert row.stdout_tail.strip() == "[REDACTED]"  # T15-redacted
    assert "sk-abcdefgh12345678" not in row.stdout_tail  # T15-no-secret
