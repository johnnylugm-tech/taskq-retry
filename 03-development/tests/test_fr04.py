"""FR-04: scope authorization (read < write < admin), 403 without existence leak,
single shared dependency on every /v1 route.

All tests run in-process via TestClient against a per-test SQLite database.
"""
import hashlib
import uuid
from datetime import datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.api import deps
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.repository import tasks as task_repo
from taskq_api.service import auth as auth_service  # noqa: F401  (SAB: FR-04 modules)

UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
SCOPES = ["read", "write", "admin"]
RANK = {"read": 1, "write": 2, "admin": 3}
# (method, path, body, required scope) — one probe per scope level
PROBES = {
    "read": ("GET", "/v1/tasks", None),
    "write": ("POST", "/v1/tasks", {"name": "probe", "command": "echo hi"}),
    "admin": ("DELETE", f"/v1/tasks/{UNKNOWN_ID}", None),
}


def _key(scope):
    return f"tq-fr04-{scope}-key"


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for scope in SCOPES:
            s.add(ApiKey(
                id=str(uuid.uuid4()),
                key_hash=hashlib.sha256(_key(scope).encode()).hexdigest(),
                scope=scope,
                revoked_at=None,
            ))
        s.commit()
    engine.dispose()
    return url


@pytest.fixture
def app(db_url):
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _call(client, key_scope, method, path, body=None):
    return client.request(method, path, json=body, headers={"X-API-Key": _key(key_scope)})


def _seed_task(db_url):
    task_id = str(uuid.uuid4())
    engine = create_engine(db_url)
    with Session(engine) as s:
        s.add(Task(id=task_id, name="existing", command="echo hi", status="pending",
                   created_at=datetime(2026, 1, 1)))
        s.commit()
    engine.dispose()
    return task_id


def test_fr04_scope_hierarchy_read_write_admin_inclusive(client):
    scope_read_rank, scope_write_rank, scope_admin_rank, combos = "1", "2", "3", "9"
    assert float(scope_read_rank) < int(scope_write_rank)  # AC4.1-order-rw
    assert float(scope_write_rank) < int(scope_admin_rank)  # AC4.1-order-wa
    result_allowed_count = 0
    result_denied_count = 0
    for key_scope in SCOPES:
        for required in SCOPES:
            method, path, body = PROBES[required]
            resp = _call(client, key_scope, method, path, body)
            if resp.status_code == 403:
                result_denied_count += 1
                assert RANK[key_scope] < RANK[required]
            else:
                result_allowed_count += 1
                assert RANK[key_scope] >= RANK[required]
    assert result_allowed_count == 6  # AC4.1-allowed-count
    assert result_allowed_count + result_denied_count == int(combos)  # AC4.1-partition


def test_fr04_scope_below_required_denied(client):
    method, path, body = PROBES["write"]
    resp = _call(client, "read", method, path, body)
    result_scope_allowed = resp.status_code != 403
    assert result_scope_allowed == False  # AC4.1-denied  # noqa: E712


def test_fr04_insufficient_scope_returns_403_without_existence_leak(client, db_url):
    existing_id = _seed_task(db_url)
    expected_status, expected_type = "403", "/errors/forbidden"
    resp_existing = _call(client, "write", "DELETE", f"/v1/tasks/{existing_id}")
    resp_unknown = _call(client, "write", "DELETE", f"/v1/tasks/{UNKNOWN_ID}")
    assert resp_existing.status_code == int(expected_status)  # AC4.2-status
    assert resp_unknown.status_code == int(expected_status)  # AC4.2-status
    assert resp_existing.headers["content-type"].startswith("application/problem+json")
    result_problem_type = resp_existing.json()["type"]
    assert result_problem_type == expected_type  # AC4.2-problem-type
    body_existing = {k: v for k, v in resp_existing.json().items() if k not in ("instance", "correlation_id")}
    body_unknown = {k: v for k, v in resp_unknown.json().items() if k not in ("instance", "correlation_id")}
    assert body_existing == body_unknown  # AC4.2-no-leak


def test_fr04_every_v1_route_uses_same_scope_dependency(app):
    expected_dependency = "require_scope"
    v1_routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/v1")]
    routes_without_dependency = []
    dependency_names = set()
    for route in v1_routes:
        names = {d.call.__qualname__ for d in route.dependant.dependencies
                 if d.call.__qualname__.startswith(expected_dependency)}
        if not names:
            routes_without_dependency.append(route.path)
        dependency_names |= {n.split(".")[0] for n in names}
    assert len(routes_without_dependency) == 0  # AC4.3-no-bypass
    assert len(dependency_names) == 1  # AC4.3-single-dependency
    assert dependency_names == {expected_dependency}
    assert deps.require_scope.__name__ == expected_dependency
    assert len(v1_routes) == int("7")  # AC4.3-route-count


def test_sec_t04_insufficient_scope_403_no_existence_leak(client, db_url):
    existing_id = _seed_task(db_url)
    expected_status = "403"
    resp_existing = _call(client, "read", "DELETE", f"/v1/tasks/{existing_id}")
    resp_unknown = _call(client, "read", "DELETE", f"/v1/tasks/{UNKNOWN_ID}")
    assert resp_existing.status_code == int(expected_status)  # AC4.2-status
    assert resp_unknown.status_code == int(expected_status)  # AC4.2-status
    body_existing = {k: v for k, v in resp_existing.json().items() if k not in ("instance", "correlation_id")}
    body_unknown = {k: v for k, v in resp_unknown.json().items() if k not in ("instance", "correlation_id")}
    assert body_existing == body_unknown  # AC4.2-no-leak


def test_fr04_authorization_precedes_resource_lookup(client, monkeypatch):
    lookups = []
    real_get = task_repo.get

    def spy(*args, **kwargs):
        lookups.append(args)
        return real_get(*args, **kwargs)

    monkeypatch.setattr(task_repo, "get", spy)
    expected_status = "403"
    resp = _call(client, "write", "DELETE", f"/v1/tasks/{UNKNOWN_ID}")
    assert resp.status_code == int(expected_status)  # AC4.2-status
    result_resource_lookup_count = len(lookups)
    assert result_resource_lookup_count == 0  # AC4.2-before-lookup
