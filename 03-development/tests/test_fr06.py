"""FR-06: persistence layer and transaction boundary (AC-6.1 .. AC-6.5, SEC T-10)."""
import ast
import hashlib
import re
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.repository import unit_of_work  # noqa: F401  (SAB: FR-06 module)
from taskq_api.repository.session import create_db_engine, get_session, session_scope

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "taskq_api"
READ_KEY = "read-key-0000000000000000"


def _py_files(exclude_layers=()):
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT)
        if rel.parts[0] in exclude_layers:
            continue
        yield rel, path


def _engine(tmp_path, monkeypatch, pool_size="5"):
    monkeypatch.setenv("TASKQ_DB_POOL_SIZE", pool_size)
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    return engine


def _count_tasks(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()


def test_fr06_data_access_only_through_repository_layer():
    violations = []
    session_holders = 0
    # models/ declares the ORM mapping and must import sqlalchemy; repository/ is the access layer.
    for rel, path in _py_files(exclude_layers=("repository", "models")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "sqlalchemy" or n.startswith("sqlalchemy.") for n in names):
                violations.append(f"{rel}:{node.lineno}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Session":
                session_holders += 1
            if isinstance(node, ast.alias) and node.name == "Session":
                session_holders += 1
    result_violations = violations
    result_session_holders_outside_repository = session_holders
    assert len(result_violations) == 0, result_violations  # AC6.1-no-leak
    assert result_session_holders_outside_repository == 0  # AC6.1-no-session-holders


def test_fr06_session_scope_commits_on_success_and_rolls_back_on_exception(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    rows_written = "1"
    captured = {}
    with pytest.raises(RuntimeError):
        with session_scope(engine) as session:
            captured["session"] = session
            for i in range(int(rows_written)):
                session.add(Task(command="echo hi", name=f"rolled-back-{i}"))
            session.flush()
            raise RuntimeError("boom")
    result_committed_rows = _count_tasks(engine)
    result_session_open = captured["session"].in_transaction()
    assert result_committed_rows == 0  # AC6.2-rollback
    assert result_session_open == False  # AC6.2-session-closed  # noqa: E712


def test_fr06_session_scope_commits_on_success(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    rows_written = "1"
    with session_scope(engine) as session:
        assert isinstance(session, Session)
        for i in range(int(rows_written)):
            session.add(Task(command="echo hi", name=f"committed-{i}"))
    result_committed_rows = _count_tasks(engine)
    result_session_open = session.in_transaction()
    assert result_committed_rows == int(rows_written)  # AC6.2-commit
    assert result_session_open == False  # AC6.2-session-closed  # noqa: E712


def test_fr06_no_string_built_sql_in_source():
    sql_re = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WHERE|ORDER\s+BY|LIMIT|VALUES)\b", re.I)

    def has_sql(node):
        return any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and sql_re.search(n.value)
            for n in ast.walk(node)
        )

    hits = []
    for rel, path in _py_files():
        for node in ast.walk(ast.parse(path.read_text())):
            built = (
                isinstance(node, ast.JoinedStr)  # f-string
                or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)))
                or (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "format")
            )
            if built and has_sql(node):
                hits.append(f"{rel}:{node.lineno}")
    result_sql_string_hits = len(hits)
    assert result_sql_string_hits == 0, hits  # AC6.3-no-string-sql


def test_fr06_relationship_queries_use_explicit_eager_loading(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    # sqlalchemy is a legal import in tests; the point is to count SELECTs issued per list call.
    from taskq_api.repository import tasks as repo_tasks

    def selects_for(n):
        with session_scope(engine) as session:
            session.query(Task).delete()
        with session_scope(engine) as session:
            for i in range(n):
                session.add(Task(command="echo", name=f"t{n}-{i}"))
        statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        with session_scope(engine) as session:
            for task in repo_tasks.list_page(session, None, n, None):
                _ = [r.id for r in getattr(task, "results", [])]
        event.remove(engine, "before_cursor_execute", _count)
        return len(statements)

    rows_small, rows_large = "5", "50"
    result_select_count_small = selects_for(int(rows_small))
    result_select_count_large = selects_for(int(rows_large))
    assert float(rows_large) > int(rows_small)  # AC6.4-sizes
    assert result_select_count_large == result_select_count_small  # AC6.4-constant-select
    result_n_plus_one_detected = 0
    # AC6.4-no-n-plus-one: every relationship() in models must declare explicit eager loading
    for path in (SRC_ROOT / "models").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "relationship":
                lazy = {k.arg: getattr(k.value, "value", None) for k in node.keywords}.get("lazy")
                if lazy not in ("selectin", "joined"):
                    result_n_plus_one_detected += 1
    assert result_n_plus_one_detected == 0  # AC6.4-no-n-plus-one


def test_fr06_engine_pool_size_and_pre_ping_configured(tmp_path, monkeypatch):
    pool_size_env = "7"
    monkeypatch.setenv("TASKQ_DB_POOL_SIZE", pool_size_env)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    result_pool_size = engine.pool.size()
    result_pool_pre_ping = engine.pool._pre_ping
    assert result_pool_size == int(pool_size_env)  # AC6.5-pool-size
    assert result_pool_pre_ping == True  # AC6.5-pre-ping  # noqa: E712


def test_fr06_db_unavailable_returns_503_problem_json(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with session_scope(engine) as s:
        s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(READ_KEY.encode()).hexdigest(), scope="read"))
    engine.dispose()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        # db_available="false": point the app at a path whose directory does not exist
        client.app.state.engine = create_db_engine(f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}")
        resp = client.get("/v1/tasks", headers={"X-API-Key": READ_KEY})
    expected_status, expected_type = "503", "/errors/not-ready"
    result_status_code = resp.status_code
    result_problem_type = resp.json()["type"]
    assert result_status_code == int(expected_status)  # NP07-status
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert result_problem_type == expected_type  # NP07-problem-type


def test_sec_t10_session_released_on_exception(tmp_path, monkeypatch):
    pool_size_env, failing_requests = "2", 5
    engine = _engine(tmp_path, monkeypatch, pool_size=pool_size_env)
    assert float(failing_requests) > int(pool_size_env)  # T10-over-pool
    for _ in range(failing_requests):
        with pytest.raises(ValueError):
            with session_scope(engine) as session:
                session.execute(text("SELECT 1"))
                raise ValueError("request failed")
    result_checked_out_connections = engine.pool.checkedout()
    assert result_checked_out_connections == 0  # T10-released


def test_fr06_unit_of_work_commits_and_guards_use_outside_with(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    uow = unit_of_work.UnitOfWork(engine)
    with pytest.raises(RuntimeError):
        uow.get_run("missing")  # used outside its 'with' block
    with uow as active:
        assert active is uow
        assert active.get_run(str(uuid.uuid4())) is None
    with pytest.raises(ValueError):
        with unit_of_work.UnitOfWork(engine):
            raise ValueError("boom")

    # request-scoped dependency: exactly one Session per request, rolled back and released on failure
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(engine=engine)))
    dependency = get_session(request)
    request_session = next(dependency)
    request_session.add(Task(command="echo hi", name="request-rolled-back"))
    request_session.flush()
    with pytest.raises(ValueError):
        dependency.throw(ValueError("request failed"))
    assert request_session.in_transaction() is False
    assert _count_tasks(engine) == 0
    assert engine.pool.checkedout() == 0
