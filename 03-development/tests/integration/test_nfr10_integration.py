"""NFR-10: integration suite driven through httpx.AsyncClient(transport=ASGITransport(app)).

Every test builds the real app against a SQLite file migrated by Alembic, so the
request path (rate limit, auth, services, repositories, executor, migrations) runs
end to end. Handlers are never called directly.
"""
import asyncio
import hashlib
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.task import Task
from taskq_api.repository import results as results_repo

_MIGRATIONS = Path(__file__).resolve().parents[2] / "src" / "migrations"
KEYS = {"read": "tq-int-read", "write": "tq-int-write", "admin": "tq-int-admin"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
PROBLEM = "application/problem+json"


def _cfg(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def _seed_keys(url: str) -> None:
    engine = create_engine(url)
    with Session(engine) as s:
        for scope, raw in KEYS.items():
            s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(), scope=scope))
        s.commit()
    engine.dispose()


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    """Alembic-migrated SQLite file with one key per scope; env wired for create_app()."""
    path = tmp_path / "taskq.db"
    command.upgrade(_cfg(path), "head")
    url = f"sqlite:///{path}"
    _seed_keys(url)
    monkeypatch.setenv("TASKQ_DB_URL", url)
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "30")
    monkeypatch.setenv("TASKQ_DRAIN_TIMEOUT", "5")
    return path


@asynccontextmanager
async def _client(app, raise_app_exceptions=True):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _h(scope):
    return {"X-API-Key": KEYS[scope]}


def _assert_problem(resp, status, type_):
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith(PROBLEM)
    body = resp.json()
    assert body["type"] == type_
    assert body["status"] == status
    assert body["correlation_id"] == resp.headers["X-Correlation-Id"]


async def _wait_run(client, task_id, run_id, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        runs = (await client.get(f"/v1/tasks/{task_id}/runs", headers=_h("read"))).json()["items"]
        row = next(r for r in runs if r["run_id"] == run_id)
        if row["status"] not in ("pending", "running"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


def test_nfr10_crud_chain_create_get_list_run_delete(db_file):
    async def scenario():
        async with _client(create_app()) as c:
            created = await c.post("/v1/tasks", json={"name": "chain", "command": "echo hello"}, headers=_h("write"))
            assert created.status_code == 201
            task_id = created.json()["id"]

            got = await c.get(f"/v1/tasks/{task_id}", headers=_h("read"))
            assert got.status_code == 200 and got.json()["name"] == "chain"

            listed = await c.get("/v1/tasks", params={"status": "pending", "limit": 1}, headers=_h("read"))
            assert listed.status_code == 200
            assert [t["id"] for t in listed.json()["items"]] == [task_id]

            accepted = await c.post(f"/v1/tasks/{task_id}/run", headers=_h("write"))
            assert accepted.status_code == 202
            run = await _wait_run(c, task_id, accepted.json()["run_id"])
            assert run["status"] == "done"
            assert run["exit_code"] == 0
            assert run["stdout_tail"].strip() == "hello"

            metrics = await c.get("/v1/metrics", headers=_h("admin"))
            assert metrics.status_code == 200
            assert metrics.json()["rate_limit_rejections"] == 0

            deleted = await c.delete(f"/v1/tasks/{task_id}", headers=_h("admin"))
            assert deleted.status_code == 204
            gone = await c.get(f"/v1/tasks/{task_id}", headers=_h("read"))
            _assert_problem(gone, 404, "/errors/not-found")

    asyncio.run(scenario())


def test_nfr10_pagination_walks_every_task_once(db_file):
    async def scenario():
        async with _client(create_app()) as c:
            ids = []
            for i in range(5):
                r = await c.post("/v1/tasks", json={"name": f"page-{i}", "command": "echo hi"}, headers=_h("write"))
                ids.append(r.json()["id"])
            seen, cursor = [], None
            for _ in range(10):
                params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
                page = (await c.get("/v1/tasks", params=params, headers=_h("read"))).json()
                seen += [t["id"] for t in page["items"]]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert sorted(seen) == sorted(ids)
            assert len(seen) == len(set(seen))

    asyncio.run(scenario())


def test_nfr10_run_failure_and_timeout_states(db_file, monkeypatch):
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.5")

    async def scenario():
        async with _client(create_app()) as c:
            async def run_command(command):
                t = (await c.post("/v1/tasks", json={"name": str(uuid.uuid4()), "command": command},
                                  headers=_h("write"))).json()["id"]
                rid = (await c.post(f"/v1/tasks/{t}/run", headers=_h("write"))).json()["run_id"]
                return await _wait_run(c, t, rid)

            failed = await run_command("false")
            assert failed["status"] == "failed" and failed["exit_code"] == 1
            missing = await run_command("definitely-not-a-real-binary-xyz")
            assert missing["status"] == "failed" and missing["exit_code"] is None
            slow = await run_command("sleep 5")
            assert slow["status"] == "timeout"

    asyncio.run(scenario())


def test_nfr10_error_codes_401_403_404_409_422(db_file):
    async def scenario():
        async with _client(create_app()) as c:
            body = {"name": "dup", "command": "echo hi"}
            _assert_problem(await c.get(f"/v1/tasks/{UNKNOWN_ID}"), 401, "/errors/unauthenticated")
            bad = await c.get(f"/v1/tasks/{UNKNOWN_ID}", headers={"X-API-Key": "tq-forged"})
            _assert_problem(bad, 401, "/errors/unauthenticated")
            _assert_problem(await c.post("/v1/tasks", json=body, headers=_h("read")), 403, "/errors/forbidden")
            _assert_problem(await c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")), 404, "/errors/not-found")
            assert (await c.post("/v1/tasks", json=body, headers=_h("write"))).status_code == 201
            _assert_problem(await c.post("/v1/tasks", json=body, headers=_h("write")), 409, "/errors/conflict")
            empty = await c.post("/v1/tasks", json={"name": "x", "command": ""}, headers=_h("write"))
            _assert_problem(empty, 422, "/errors/validation")
            forbidden_delete = await c.delete(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("write"))
            _assert_problem(forbidden_delete, 403, "/errors/forbidden")

    asyncio.run(scenario())


def test_nfr10_rate_limit_triggers_then_recovers(db_file, monkeypatch):
    monkeypatch.setenv("TASKQ_RATE_BURST", "2")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "20")

    async def scenario():
        async with _client(create_app()) as c:
            url = f"/v1/tasks/{UNKNOWN_ID}"
            assert (await c.get(url, headers=_h("read"))).status_code == 404
            assert (await c.get(url, headers=_h("read"))).status_code == 404
            limited = await c.get(url, headers=_h("read"))
            _assert_problem(limited, 429, "/errors/rate-limited")
            assert int(limited.headers["Retry-After"]) >= 1
            await asyncio.sleep(0.2)
            assert (await c.get(url, headers=_h("read"))).status_code == 404
            metrics = await c.get("/v1/metrics", headers=_h("admin"))
            assert metrics.status_code in (200, 429)

    asyncio.run(scenario())


def test_nfr10_health_probes_and_503_when_database_down(db_file, monkeypatch, tmp_path):
    async def healthy():
        async with _client(create_app()) as c:
            assert (await c.get("/healthz")).json() == {"status": "ok"}
            assert (await c.get("/readyz")).json() == {"status": "ready"}

    asyncio.run(healthy())

    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}")

    async def down():
        async with _client(create_app()) as c:
            resp = await c.get("/readyz")
            _assert_problem(resp, 503, "/errors/not-ready")
            assert "unavailable" in resp.json()["detail"]
            api = await c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
            _assert_problem(api, 503, "/errors/not-ready")

    asyncio.run(down())


def test_nfr10_readyz_503_when_migration_not_at_head(db_file):
    command.downgrade(_cfg(db_file), "-1")

    async def scenario():
        async with _client(create_app()) as c:
            resp = await c.get("/readyz")
            _assert_problem(resp, 503, "/errors/not-ready")
            assert "migration" in resp.json()["detail"]

    asyncio.run(scenario())


def test_nfr10_migration_round_trip_preserves_data(db_file):
    cfg = _cfg(db_file)
    con = sqlite3.connect(db_file)
    task_id = str(uuid.uuid4())
    con.execute("INSERT INTO tasks (id, command, name, status, created_at) VALUES (?, 'echo hi', 'rt', 'pending', "
                "'2026-01-01 00:00:00')", (task_id,))
    con.execute("INSERT INTO task_results (id, task_id, status, exit_code, stdout_tail, stderr_tail, duration_ms, "
                "finished_at) VALUES (?, ?, 'done', 0, 'hello', '', 12, '2026-01-01 00:00:01')",
                (str(uuid.uuid4()), task_id))
    con.commit()
    before = con.execute("SELECT task_id, exit_code, stdout_tail, duration_ms FROM task_results").fetchall()
    con.close()

    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_file)
    after = con.execute("SELECT task_id, exit_code, stdout_tail, duration_ms FROM task_results").fetchall()
    con.close()
    assert after == before

    command.downgrade(cfg, "base")
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert tables - {"alembic_version"} == set()


def test_nfr10_graceful_drain_interrupts_runs_past_the_deadline(db_file, monkeypatch):
    monkeypatch.setenv("TASKQ_DRAIN_TIMEOUT", "0.3")

    async def scenario():
        app = create_app()
        async with _client(app) as c:
            task_id = (await c.post("/v1/tasks", json={"name": "drain", "command": "sleep 30"},
                                    headers=_h("write"))).json()["id"]
            with Session(app.state.engine) as s:
                results_repo.add(s, task_id, "run-drain-slow")
                results_repo.add(s, task_id, "run-drain-fast")
            app.state.executor.submit("run-drain-slow", "sleep 30")
            app.state.executor.submit("run-drain-fast", "echo bye")
            await asyncio.sleep(0.5)
        return task_id

    task_id = asyncio.run(scenario())
    con = sqlite3.connect(db_file)
    rows = dict(con.execute("SELECT id, status FROM task_results WHERE task_id = ?", (task_id,)).fetchall())
    con.close()
    assert rows == {"run-drain-slow": "interrupted", "run-drain-fast": "done"}


def test_nfr10_unexpected_exception_returns_sanitised_500(db_file):
    async def scenario():
        app = create_app()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("SELECT secret FROM users /tmp/x.py")

        async with _client(app, raise_app_exceptions=False) as c:
            resp = await c.get("/boom")
            _assert_problem(resp, 500, "/errors/internal")
            text = resp.text
            assert "SELECT" not in text and "/tmp/x.py" not in text and "Traceback" not in text

    asyncio.run(scenario())


def test_nfr10_key_cli_creates_a_key_that_authenticates(db_file, monkeypatch, capsys):
    from taskq_api.__main__ import main

    monkeypatch.setattr("sys.argv", ["taskq_api", "key", "create", "--scope", "read"])
    main()
    key = capsys.readouterr().out.strip()
    assert key.startswith("tq-")

    async def scenario():
        async with _client(create_app()) as c:
            resp = await c.get(f"/v1/tasks/{UNKNOWN_ID}", headers={"X-API-Key": key})
            _assert_problem(resp, 404, "/errors/not-found")

    asyncio.run(scenario())


def test_nfr10_task_model_defaults_are_persisted(db_file):
    engine = create_engine(f"sqlite:///{db_file}")
    with Session(engine) as s:
        s.add(Task(id=str(uuid.uuid4()), command="echo hi", name="defaults"))
        s.commit()
        row = s.query(Task).filter_by(name="defaults").one()
        assert row.status == "pending" and row.created_at is not None
    engine.dispose()
