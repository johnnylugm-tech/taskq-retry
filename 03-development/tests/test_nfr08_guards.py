"""NFR-08 guards: exact contract values that mutation testing showed were unpinned.

Covers fixed problem+json titles/details, size limits, tail truncation, duration
arithmetic, percentile rank, token-bucket retry hints and layer-neutral aliases.
"""
import asyncio
import base64
import dataclasses
import hashlib
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.api.schemas import TaskCreate
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.repository import results as results_repo
from taskq_api.repository import session as repo_session
from taskq_api.repository.unit_of_work import UnitOfWork
from taskq_api.service import executor, keys, metrics, rate_limit, runner
from taskq_api.service import tasks as task_service
from taskq_api.service import validation

RAW = {"read": "tq-g-read", "write": "tq-g-write", "admin": "tq-g-admin", "bogus": "tq-g-bogus"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


def _fields(resp):
    body = resp.json()
    return {k: body[k] for k in ("type", "title", "status", "detail")}


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'guards.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "5")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for scope, raw in RAW.items():
            s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(), scope=scope))
        s.commit()
    engine.dispose()
    return url


@pytest.fixture
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def client(db_url):
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _h(scope):
    return {"X-API-Key": RAW[scope]}


def _seed_run(engine, run_id="run-guard"):
    with Session(engine) as s:
        task = Task(name=f"t-{run_id}", command="echo hi")
        s.add(task)
        s.commit()
        results_repo.add(s, task.id, run_id)
    return run_id


def _row(engine, run_id):
    with Session(engine) as s:
        return results_repo.get(s, run_id)


def test_nfr08_problem_bodies_have_exact_titles_and_details(client):
    assert _fields(client.get(f"/v1/tasks/{UNKNOWN_ID}")) == {
        "type": "/errors/unauthenticated", "title": "Unauthenticated", "status": 401,
        "detail": "missing or invalid API key"}
    assert _fields(client.post("/v1/tasks", json={"name": "n", "command": "echo"}, headers=_h("read"))) == {
        "type": "/errors/forbidden", "title": "Forbidden", "status": 403, "detail": "insufficient scope"}
    assert _fields(client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))) == {
        "type": "/errors/not-found", "title": "Not found", "status": 404, "detail": "task not found"}
    body = {"name": "dup", "command": "echo"}
    client.post("/v1/tasks", json=body, headers=_h("write"))
    assert _fields(client.post("/v1/tasks", json=body, headers=_h("write"))) == {
        "type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": "task name already exists"}
    assert _fields(client.get("/v1/tasks", params={"cursor": "!!not-base64!!"}, headers=_h("read"))) == {
        "type": "/errors/validation", "title": "Validation error", "status": 422, "detail": "invalid cursor"}


def test_nfr08_rate_limit_problem_body_is_exact(db_url, monkeypatch):
    monkeypatch.setenv("TASKQ_RATE_BURST", "1")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "0.01")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        assert c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")).status_code == 404
        limited = c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    assert _fields(limited) == {
        "type": "/errors/rate-limited", "title": "Too Many Requests", "status": 429,
        "detail": "rate limit exceeded"}
    assert limited.headers["Retry-After"] == "100"


def test_nfr08_unknown_scope_is_rejected_and_hierarchy_holds(client):
    assert client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("bogus")).status_code == 403
    assert client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")).status_code == 404
    assert client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("write")).status_code == 404
    assert client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("admin")).status_code == 404
    assert client.delete(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("write")).status_code == 403
    assert client.delete(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("admin")).status_code == 404


def test_nfr08_readyz_problem_bodies_are_exact(db_url, tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "src" / "migrations"))
    stale = tmp_path / "stale.db"
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{stale}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{stale}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        assert _fields(c.get("/readyz")) == {
            "type": "/errors/not-ready", "title": "Service Unavailable", "status": 503,
            "detail": "migration not at head"}
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'no-such-dir' / 'x.db'}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        assert _fields(c.get("/readyz")) == {
            "type": "/errors/not-ready", "title": "Service Unavailable", "status": 503,
            "detail": "database unavailable"}


def test_nfr08_pagination_boundaries(client, monkeypatch):
    for i in range(2):
        client.post("/v1/tasks", json={"name": f"p{i}", "command": "echo"}, headers=_h("write"))
    exact = client.get("/v1/tasks", params={"limit": 2}, headers=_h("read")).json()
    assert len(exact["items"]) == 2 and exact["next_cursor"] is None
    first = client.get("/v1/tasks", params={"limit": 1}, headers=_h("read")).json()
    assert len(first["items"]) == 1 and first["next_cursor"] is not None

    seen = []
    real = task_service.repo.list_page

    def spy(session, status, limit, after):
        seen.append(limit)
        return real(session, status, limit, after)

    monkeypatch.setattr(task_service.repo, "list_page", spy)
    client.get("/v1/tasks", params={"limit": 7}, headers=_h("read"))
    assert seen == [8]


def test_nfr08_cursor_keeps_pipes_inside_the_task_id(client):
    cursor = base64.urlsafe_b64encode(b"2026-01-01T00:00:00|a|b").decode()
    resp = client.get("/v1/tasks", params={"cursor": cursor}, headers=_h("read"))
    assert resp.status_code == 200 and resp.json()["items"] == []


def test_nfr08_tail_truncates_to_4096_bytes_and_replaces_invalid_utf8():
    assert executor._tail(b"a" * 5000) == "a" * 4096
    assert executor._tail(b"\xff") == "�"


def test_nfr08_set_status_error_messages_are_exact(engine):
    with pytest.raises(RuntimeError) as missing:
        executor._set_status(engine, "ghost-run", "running")
    assert str(missing.value) == "run ghost-run not found"
    run_id = _seed_run(engine)
    with pytest.raises(RuntimeError) as illegal:
        executor._set_status(engine, run_id, "done")
    assert str(illegal.value) == "illegal run transition pending -> done"


def test_nfr08_terminal_states_allow_no_transition():
    for terminal in ("done", "failed", "timeout", "interrupted"):
        for target in ("pending", "running", "done", "failed", "timeout", "interrupted"):
            assert not runner.is_transition_allowed(terminal, target)
    assert runner.is_transition_allowed("pending", "running")
    assert not runner.is_transition_allowed("pending", "done")
    assert not runner.is_transition_allowed("nonsense", "running")


def test_nfr08_spawn_failure_leaves_stdout_empty(engine):
    run_id = _seed_run(engine)
    asyncio.run(executor.execute(engine, run_id, "/nonexistent/guard-binary", timeout=5.0))
    row = _row(engine, run_id)
    assert row.status == "failed" and row.exit_code is None
    assert row.stdout_tail == "" and row.stderr_tail != ""


def test_nfr08_duration_ms_is_elapsed_milliseconds(engine, monkeypatch):
    run_id = _seed_run(engine)
    ticks = iter([100.0, 101.0])
    monkeypatch.setattr(executor, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    asyncio.run(executor.execute(engine, run_id, "true", timeout=5.0))
    assert _row(engine, run_id).duration_ms == 1000


def test_nfr08_executor_initial_state_and_submit_guard(engine):
    ex = executor.Executor(engine, max_concurrent=1, drain_timeout=1.0, task_timeout=1.0)
    assert ex.peak_running == 0 and ex.running == 0
    with pytest.raises(AssertionError) as err:
        ex.submit("any", "true")
    assert str(err.value) == "Executor.start() must be called before submit()"


def test_nfr08_drain_gives_up_after_drain_timeout(engine):
    run_id = _seed_run(engine)

    async def scenario():
        ex = executor.Executor(engine, max_concurrent=1, drain_timeout=0.2, task_timeout=30.0)
        await ex.start()
        ex.submit(run_id, "sleep 3")
        await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        begun = loop.time()
        await ex.drain()
        return loop.time() - begun

    assert asyncio.run(scenario()) < 2.0
    assert _row(engine, run_id).status == "interrupted"


def test_nfr08_key_has_prefix_and_256_bits_of_entropy(engine):
    with Session(engine) as s:
        plaintext = keys.create_key(s, "read")
    assert plaintext.startswith("tq-") and len(plaintext) == 3 + 43


def test_nfr08_rejection_counter_counts_every_increment():
    counter = metrics.RejectionCounter()
    assert counter.count == 0
    counter.increment()
    counter.increment()
    assert counter.count == 2


def test_nfr08_percentile_uses_nearest_rank():
    assert metrics.percentile([], 50) == 0.0
    assert metrics.percentile([5], 99) == 5.0
    assert metrics.percentile(list(range(1, 102)), 50) == 51.0
    assert metrics.percentile(list(range(1, 11)), 90) == 9.0


def test_nfr08_decision_is_immutable_with_zero_default():
    allowed = rate_limit.Decision(True)
    assert allowed.retry_after == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        allowed.allowed = False


def test_nfr08_retry_after_is_ceiling_with_floor_of_one(engine, monkeypatch):
    monkeypatch.setattr(rate_limit, "time", types.SimpleNamespace(time=lambda: 5000.0))
    with Session(engine) as s:
        key_id = s.query(ApiKey).filter_by(scope="read").one().id
    with Session(engine) as s:
        assert rate_limit.consume(s, key_id, 1, 1000.0).allowed
    with Session(engine) as s:
        fast = rate_limit.consume(s, key_id, 1, 1000.0)
    assert not fast.allowed and fast.retry_after == 1
    with Session(engine) as s:
        key2 = s.query(ApiKey).filter_by(scope="write").one().id
        assert rate_limit.consume(s, key2, 1, 0.4).allowed
    with Session(engine) as s:
        slow = rate_limit.consume(s, key2, 1, 0.4)
    assert not slow.allowed and slow.retry_after == 3


def test_nfr08_validation_limits_and_forbidden_characters():
    assert validation.MAX_NAME_LEN == 255 and validation.MAX_COMMAND_LEN == 1000
    assert TaskCreate(name="n" * 255, command="echo XX").command == "echo XX"
    with pytest.raises(ValidationError):
        TaskCreate(name="n" * 256, command="echo")
    assert TaskCreate(name="n", command="c" * 1000).command == "c" * 1000
    with pytest.raises(ValidationError):
        TaskCreate(name="n", command="c" * 1001)
    for bad in ("a\x00b", "a\rb", "a\nb"):
        with pytest.raises(ValueError) as err:
            validation.validate_command(bad)
        assert str(err.value) == "command contains forbidden characters"
    assert validation.validate_command("echo ok") == "echo ok"


def test_nfr08_layer_neutral_aliases_point_at_sqlalchemy_types():
    assert repo_session.DbSession is Session
    assert repo_session.DbEngine is Engine


def test_nfr08_unit_of_work_requires_with_block(engine):
    with pytest.raises(RuntimeError) as err:
        UnitOfWork(engine).get_run("any")
    assert str(err.value) == "UnitOfWork used outside its 'with' block"


def test_nfr08_migration_env_requires_a_database_url():
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "src" / "migrations"))
    with pytest.raises(RuntimeError) as err:
        command.upgrade(cfg, "head")
    assert str(err.value) == "sqlalchemy.url is not configured"
