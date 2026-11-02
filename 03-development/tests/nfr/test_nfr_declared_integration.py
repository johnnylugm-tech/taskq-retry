"""[NFR-01..05, NFR-09, NFR-10, NFR-12] TEST_SPEC cases declared outside every FR's rows (integration layer).

Rows: 02-architecture/TEST_SPEC.md, sections NFR-01 .. NFR-05, NFR-09 (#1), NFR-10 (entrypoint wiring),
NFR-12 (deployment smoke). Everything runs against real SQLite files; HTTP is driven through
`httpx.ASGITransport` (NFR-99-7: the measured scope is the full ASGI call) or, for the smoke case,
through a real uvicorn process.
"""
import ast
import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.models.task_result import TaskResult
from taskq_api.repository import health as health_repo
from taskq_api.service import executor

_SRC = Path(__file__).resolve().parents[2] / "src"
_MIGRATIONS = _SRC / "migrations"
KEYS = {"read": "tq-nfr-read", "write": "tq-nfr-write", "admin": "tq-nfr-admin"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
FORBIDDEN = "SELECT,Traceback,.py,sqlite,CREATE TABLE".split(",")
PASSWORD = "s3cretPassw0rd"
PROBLEM = "application/problem+json"


def _h(scope):
    return {"X-API-Key": KEYS[scope]}


def _alembic_cfg(db_file, script_location=_MIGRATIONS):
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def _add_keys(session):
    for scope, raw in KEYS.items():
        session.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(), scope=scope))


def _make_db(path, rows=0, migrate=False):
    """SQLite file with one key per scope and `rows` seeded tasks; returns its URL."""
    url = f"sqlite:///{path}"
    if migrate:
        command.upgrade(_alembic_cfg(path), "head")
    engine = create_engine(url)
    if not migrate:
        Base.metadata.create_all(engine)
    base = datetime(2026, 1, 1)
    with Session(engine) as s:
        _add_keys(s)
        if rows:
            s.execute(insert(Task), [
                {"id": str(uuid.UUID(int=i + 1)), "command": "echo hi", "name": f"task-{i}",
                 "status": "pending", "created_at": base + timedelta(seconds=i)}
                for i in range(rows)])
        s.commit()
    engine.dispose()
    return url


@contextlib.contextmanager
def _asgi(app):
    """Synchronous handle `call(method, path, **kw)` over httpx.ASGITransport with the app lifespan running."""
    loop = asyncio.new_event_loop()
    lifespan = app.router.lifespan_context(app)
    loop.run_until_complete(lifespan.__aenter__())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://test")
    try:
        yield lambda method, path, **kw: loop.run_until_complete(client.request(method, path, **kw))
    finally:
        loop.run_until_complete(client.aclose())
        loop.run_until_complete(lifespan.__aexit__(None, None, None))
        loop.close()


def _p95_ms(benchmark):
    data = sorted(benchmark.stats.stats.data)
    return data[math.ceil(0.95 * len(data)) - 1] * 1000.0


# ---------------------------------------------------------------- NFR-01

@pytest.fixture(scope="module")
def big_db(tmp_path_factory):
    """[NFR-01] 10000-row read-only seed, built once (state_mode=shared)."""
    path = tmp_path_factory.mktemp("nfr01") / "big.db"
    return _make_db(path, rows=10000)


def _benchmark_get(benchmark, big_db, monkeypatch, path, **params):
    monkeypatch.setenv("TASKQ_DB_URL", big_db)
    with _asgi(create_app()) as call:
        assert call("GET", path, headers=_h("read"), params=params).status_code == 200
        benchmark.pedantic(lambda: call("GET", path, headers=_h("read"), params=params),
                           rounds=100, iterations=1, warmup_rounds=3)
        return _p95_ms(benchmark)


def test_nfr01_get_task_p95_under_30ms_at_10k_rows(benchmark, big_db, monkeypatch):
    rows, p95_budget_ms = "10000", "30"
    task_id = str(uuid.UUID(int=5000))
    result_p95_ms = _benchmark_get(benchmark, big_db, monkeypatch, f"/v1/tasks/{task_id}")
    engine = create_engine(big_db)
    with Session(engine) as s:
        seeded = s.query(Task).count()
    engine.dispose()
    assert result_p95_ms < int(p95_budget_ms)  # N1.1-p95
    assert float(seeded) == float(rows)  # N1.1-rows


def test_nfr01_list_tasks_limit_50_p95_under_80ms_at_10k_rows(benchmark, big_db, monkeypatch):
    rows, limit, p95_budget_ms = "10000", "50", "80"
    result_p95_ms = _benchmark_get(benchmark, big_db, monkeypatch, "/v1/tasks", limit=int(limit))
    engine = create_engine(big_db)
    with Session(engine) as s:
        seeded = s.query(Task).count()
    engine.dispose()
    assert result_p95_ms < int(p95_budget_ms)  # N1.1-p95
    assert float(seeded) == float(rows)  # N1.1-rows


def _list_statement_count(db_url, monkeypatch, limit):
    monkeypatch.setenv("TASKQ_DB_URL", db_url)
    app = create_app()
    statements = []
    event.listen(app.state.engine, "before_cursor_execute", lambda *a, **k: statements.append(a[2]))
    with _asgi(app) as call:
        call("GET", "/v1/tasks", headers=_h("read"), params={"limit": limit})  # warm-up: first bucket insert
        statements.clear()
        resp = call("GET", "/v1/tasks", headers=_h("read"), params={"limit": limit})
    assert resp.status_code == 200 and len(resp.json()["items"]) == limit
    return len(statements)


def test_nfr01_list_sql_statement_count_constant_regardless_of_rows(tmp_path, monkeypatch):
    rows_small, rows_large, limit = "100", "10000", "50"
    small = _make_db(tmp_path / "small.db", rows=int(rows_small))
    large = _make_db(tmp_path / "large.db", rows=int(rows_large))
    result_statement_count_small = _list_statement_count(small, monkeypatch, int(limit))
    result_statement_count_large = _list_statement_count(large, monkeypatch, int(limit))
    assert result_statement_count_small > 0
    assert result_statement_count_large == result_statement_count_small  # N1.3-constant
    assert float(rows_large) > int(rows_small)  # N1.3-sizes


# ---------------------------------------------------------------- NFR-02

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Seeded SQLite file, TASKQ_DB_URL set (state_mode=isolate_per_test)."""
    url = _make_db(tmp_path / "taskq.db")
    monkeypatch.setenv("TASKQ_DB_URL", url)
    return url


def _normalised(resp, task_id):
    body = dict(resp.json())
    body.pop("correlation_id")
    body["instance"] = body["instance"].replace(task_id, "{id}")
    return body


def test_nfr02_403_body_does_not_reveal_resource_existence(env):
    expected_status = "403"
    with TestClient(create_app()) as c:
        existing = c.post("/v1/tasks", json={"name": "keep", "command": "echo hi"}, headers=_h("write")).json()["id"]
        r_existing = c.delete(f"/v1/tasks/{existing}", headers=_h("write"))
        r_unknown = c.delete(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("write"))
    assert r_existing.status_code == r_unknown.status_code == int(expected_status)  # N2.4-status
    result_body_existing = _normalised(r_existing, existing)
    result_body_unknown = _normalised(r_unknown, UNKNOWN_ID)
    assert result_body_existing == result_body_unknown  # N2.4-no-leak


def test_nfr02_error_body_has_no_stack_sql_or_path(env):
    expected_status = "500"
    app = create_app()

    @app.get("/boom")
    async def _boom():
        raise RuntimeError("SELECT * FROM tasks; Traceback (most recent call last): File /srv/app/x.py "
                           "CREATE TABLE tasks sqlite")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    result_status_code = r.status_code
    result_detail = r.text
    assert result_status_code == int(expected_status)  # N2.4-status
    assert all(frag not in result_detail for frag in FORBIDDEN)  # N2.5-no-internals


def test_nfr02_422_body_has_no_stack_sql_or_path(env):
    expected_status = "422"
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.post("/v1/tasks", json={"name": "empty-cmd", "command": ""}, headers=_h("write"))
    result_status_code = r.status_code
    result_detail = r.text
    assert result_status_code == int(expected_status)  # N2.4-status
    assert all(frag not in result_detail for frag in FORBIDDEN)  # N2.5-no-internals


# ---------------------------------------------------------------- NFR-03

def test_nfr03_db_failure_yields_readyz_503_without_infinite_retry(env, monkeypatch):
    expected_status, max_wall_seconds = "503", "5"
    pings = []
    real_ping = health_repo.ping
    monkeypatch.setattr(health_repo, "ping", lambda engine: (pings.append(1), real_ping(engine))[1])
    app = create_app()
    app.state.engine = create_engine("sqlite:////nonexistent-dir-nfr03/taskq.db")  # db_available=false
    t0 = time.monotonic()
    with TestClient(app) as c:
        r = c.get("/readyz")
    result_elapsed_seconds = time.monotonic() - t0
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)  # N3.4-status
    assert result_elapsed_seconds < float(max_wall_seconds)  # N3.4-bounded
    assert "database" in r.json()["detail"] and len(pings) == 1  # one attempt: no silent retry loop


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_nfr03_timeout_leaves_no_orphan_subprocess(tmp_path, monkeypatch):
    command_line, task_timeout_seconds = "sleep 30", "0.5"
    url = f"sqlite:///{tmp_path / 'orphan.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    spawned = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        proc = await real(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    run_id = str(uuid.uuid4())
    with Session(engine) as s:
        task = Task(command=command_line, name="orphan-check")
        s.add(task)
        s.flush()
        s.add(TaskResult(id=run_id, task_id=task.id, status="pending"))
        s.commit()
    t0 = time.monotonic()
    asyncio.run(executor.execute(engine, run_id, command_line, float(task_timeout_seconds)))
    with Session(engine) as s:
        result_final_status = s.get(TaskResult, run_id).status
    engine.dispose()
    result_orphan_pids = sum(1 for p in spawned if _alive(p.pid))
    assert len(spawned) == 1 and time.monotonic() - t0 < 10
    assert result_orphan_pids == 0  # N3.5-no-orphan
    assert result_final_status == "timeout"  # N3.5-timeout-status


def _tables(db_file):
    con = sqlite3.connect(db_file)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_nfr03_failed_migration_rolls_back_to_previous_revision(tmp_path):
    start_revision, expected_revision_after = "v2", "v2"
    db_file = tmp_path / "fail.db"
    scripts = tmp_path / "migrations_with_injected_failure"
    shutil.copytree(_MIGRATIONS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    v3 = scripts / "versions" / "v3_split_results.py"
    source = v3.read_text()
    hook = "        _move_json_to_results(op.get_bind(), results)\n"
    assert hook in source
    v3.write_text(source.replace(hook, hook + '        raise RuntimeError("injected failure after the v3 data move")\n'))
    cfg = _alembic_cfg(db_file, scripts)
    command.upgrade(cfg, start_revision)
    con = sqlite3.connect(db_file)
    con.execute("INSERT INTO tasks (id, command, name, status, created_at, result_json) "
                "VALUES ('t1', 'echo hi', 'moved', 'done', '2026-01-01 00:00:00', '{\"exit_code\": 0}')")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="injected failure"):
        command.upgrade(cfg, "v3")
    con = sqlite3.connect(db_file)
    result_revision_after = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    result_task_results_table_exists = "task_results" in _tables(db_file)
    kept = con.execute("SELECT result_json FROM tasks WHERE id = 't1'").fetchone()
    con.close()
    assert result_revision_after == expected_revision_after  # N3.6-revision
    assert result_task_results_table_exists == False  # noqa: E712  # N3.6-no-v3-table
    assert kept == ('{"exit_code": 0}',)


# ---------------------------------------------------------------- NFR-04

def _secret_env(tmp_path, monkeypatch, reachable):
    """TASKQ_DB_URL carrying the password in its query string; the database is reachable or not."""
    if reachable:
        path = tmp_path / "taskq.db"
        _make_db(path)
    else:
        path = Path("/nonexistent-dir-nfr04/taskq.db")
    url = f"sqlite:///{path}?password={PASSWORD}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    return url


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_nfr04_db_url_password_absent_from_logs_errors_and_metrics(tmp_path, monkeypatch, caplog, capfd):
    db_url_password = PASSWORD
    _secret_env(tmp_path, monkeypatch, reachable=False)
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        ready = c.get("/readyz")
        api = c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    result_log_text = caplog.text + capfd.readouterr().err + ready.text + api.text
    assert ready.status_code == 503 and api.status_code == 503  # the failure path was exercised
    assert db_url_password not in result_log_text  # N4.2-logs


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_nfr04_db_url_password_absent_from_error_body(tmp_path, monkeypatch):
    db_url_password = PASSWORD
    _secret_env(tmp_path, monkeypatch, reachable=False)  # trigger: database unavailable
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        responses = [c.get("/readyz"), c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")),
                     c.post("/v1/tasks", json={"name": "x", "command": "echo hi"}, headers=_h("write"))]
    result_error_body_text = "".join(r.text + str(dict(r.headers)) for r in responses)
    assert all(r.status_code == 503 and r.headers["content-type"].startswith(PROBLEM) for r in responses)
    assert db_url_password not in result_error_body_text  # N4.2-error-body


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_nfr04_db_url_password_absent_from_metrics_response(tmp_path, monkeypatch):
    db_url_password = PASSWORD
    _secret_env(tmp_path, monkeypatch, reachable=True)
    with TestClient(create_app()) as c:
        c.post("/v1/tasks", json={"name": "m", "command": "echo hi"}, headers=_h("write"))
        metrics = c.get("/v1/metrics", headers=_h("admin"))
    result_metrics_body = metrics.text
    assert metrics.status_code == 200
    assert db_url_password not in result_metrics_body  # N4.2-metrics


def test_nfr04_api_key_plaintext_not_persisted_anywhere(tmp_path):
    db_file = tmp_path / "nfr04.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    sub_env = {**os.environ, "TASKQ_DB_URL": f"sqlite:///{db_file}",
               "PYTHONPATH": str(_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run([sys.executable, "-m", "taskq_api", "key", "create", "--scope", "write"],
                          env=sub_env, capture_output=True, text=True, timeout=7)
    assert proc.returncode == 0, proc.stderr
    plaintext = proc.stdout.strip()
    assert plaintext.startswith("tq-")
    persisted = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    result_plaintext_occurrences_in_db_file = persisted.count(plaintext.encode())
    result_plaintext_occurrences_in_logs = proc.stderr.count(plaintext)
    result_plaintext_occurrences_in_stdout = proc.stdout.count(plaintext)
    assert hashlib.sha256(plaintext.encode()).hexdigest().encode() in persisted  # only the hash is stored
    assert result_plaintext_occurrences_in_db_file == 0  # N4.3-db
    assert result_plaintext_occurrences_in_logs == 0  # N4.3-logs
    assert result_plaintext_occurrences_in_stdout == 1  # N4.3-stdout-once


# ---------------------------------------------------------------- NFR-05

def test_nfr05_openapi_every_endpoint_has_summary_and_description(env):
    expected_operations, expected_status = "9", "200"
    with _asgi(create_app()) as call:
        resp = call("GET", "/openapi.json")
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    operations = [(m, p, op) for p, item in resp.json()["paths"].items() for m, op in item.items() if m in methods]
    result_status_code = resp.status_code
    result_operation_count = len(operations)
    result_operations_missing_summary = [(m, p) for m, p, op in operations if not op.get("summary")]
    result_operations_missing_description = [(m, p) for m, p, op in operations if not op.get("description")]
    assert result_status_code == int(expected_status)  # N5.2-status
    assert result_operation_count == int(expected_operations)  # N5.2-count
    assert len(result_operations_missing_summary) == 0  # N5.2-summary
    assert len(result_operations_missing_description) == 0  # N5.2-description


# ---------------------------------------------------------------- NFR-09

def test_nfr09_migration_tests_use_real_sqlite_file():
    expected_url_scheme, expected_file_suffix, migration_test_modules = "sqlite:" + "///", ".db", "3"
    tests = Path(__file__).resolve().parents[1]
    modules = [p for p in sorted(tests.rglob("test_*.py"))
               if re.search(r"command\.(upgrade|downgrade)\(", p.read_text())]
    in_memory, file_backed, suffixes = 0, 0, set()
    for module in modules:
        text = module.read_text()
        bare = ("sqlite:" + "//", expected_url_scheme)  # a URL with no database path is an in-memory engine
        tree = ast.parse(text)
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(n)}
        docstrings |= {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}  # f-string parts
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        in_memory += sum(1 for v in literals if v in bare or ":mem" + "ory:" in v)
        names = re.findall(r"""["']([\w.-]+\.db)["']""", text)
        file_backed += 1 if f"{expected_url_scheme}{{" in text and names else 0
        suffixes.update(Path(n).suffix for n in names)
    result_in_memory_url_count = in_memory
    result_file_backed_url_count = file_backed
    result_db_file_suffix = suffixes.pop() if len(suffixes) == 1 else suffixes
    assert result_in_memory_url_count == 0  # N9.5-no-memory
    # Declared count is 3 (FR-07, NFR-10 integration, NFR-03); test_nfr08_guards.py later added a fourth migration
    # module, so the predicate is: every module that runs migrations is file-backed, and at least 3 of them exist.
    assert len(modules) >= int(migration_test_modules)
    assert result_file_backed_url_count == len(modules)  # N9.5-file-backed
    assert result_db_file_suffix == expected_file_suffix  # N9.5-suffix


# ---------------------------------------------------------------- NFR-10 wiring / NFR-12 smoke

def test_e2e_entrypoint_request_traverses_auth_rate_limit_repository_and_error_contract(tmp_path, monkeypatch):
    expected_status, second_expected_status = "201", "401"
    db_file = tmp_path / "wiring.db"
    monkeypatch.setenv("TASKQ_DB_URL", _make_db(db_file, migrate=True))
    with _asgi(create_app()) as call:
        first = call("POST", "/v1/tasks", json={"name": "wiring-check", "command": "echo wired"}, headers=_h("write"))
        second = call("POST", "/v1/tasks", json={"name": "wiring-check-2", "command": "echo wired"})
    con = sqlite3.connect(db_file)
    result_task_rows = con.execute("SELECT count(*) FROM tasks WHERE name = 'wiring-check'").fetchone()[0]
    result_bucket_row_count = con.execute("SELECT count(*) FROM rate_buckets").fetchone()[0]
    con.close()
    assert first.status_code == int(expected_status)  # E2E-created
    assert result_task_rows == 1  # E2E-row-persisted
    assert result_bucket_row_count == 1  # E2E-bucket-row
    assert second.status_code == int(second_expected_status)  # E2E-error-contract
    assert second.headers["content-type"].startswith(PROBLEM)  # E2E-error-content-type


def test_deploy_smoke_service_starts_and_healthz_readyz_return_200(tmp_path):
    host, port = "127.0.0.1", 18765
    healthz_expected_status, readyz_expected_status = "200", "200"
    db_file = tmp_path / "smoke.db"
    command.upgrade(_alembic_cfg(db_file), "head")
    sub_env = {**os.environ, "TASKQ_DB_URL": f"sqlite:///{db_file}", "TASKQ_LOG_LEVEL": "WARNING",
               "PYTHONPATH": str(_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    # `taskq_api.app:app` is not a module attribute: the app is a factory, started as `make verify-system` does.
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "taskq_api.app:create_app", "--factory",
                             "--host", host, "--port", str(port), "--log-level", "warning"],
                            env=sub_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    statuses = {}
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and "/healthz" not in statuses:
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1) as r:
                    statuses["/healthz"] = r.status
            except OSError:
                time.sleep(0.1)
        with urllib.request.urlopen(f"http://{host}:{port}/readyz", timeout=1) as r:
            statuses["/readyz"] = r.status
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    with socket.socket() as probe:
        result_server_stopped = proc.poll() is not None and probe.connect_ex((host, port)) != 0
    assert statuses.get("/healthz") == int(healthz_expected_status)  # SMOKE-healthz
    assert statuses.get("/readyz") == int(readyz_expected_status)  # SMOKE-readyz
    assert result_server_stopped == True  # noqa: E712  # SMOKE-stopped
