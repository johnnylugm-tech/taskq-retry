"""FR-10: RFC 7807 error contract (problem+json, fixed fields, no leaks,
correlation id in header and log, CancelledError propagation, CORS default deny).

Runs in-process via TestClient against a per-test SQLite database.
"""
import asyncio
import hashlib
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.api import error_handlers  # noqa: F401  (SAB: FR-10 module)
from taskq_api.api import middleware  # noqa: F401  (SAB: FR-10 module)
from taskq_api import errors  # noqa: F401  (SAB: FR-10 module)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base

KEYS = {"read": "tq-fr10-read", "write": "tq-fr10-write", "admin": "tq-fr10-admin"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
FORBIDDEN_FRAGMENTS = "SELECT,Traceback,.py,sqlite,CREATE TABLE".split(",")
REQUIRED_FIELDS = "type,title,status,detail,instance,correlation_id".split(",")
PROBLEM = "application/problem+json"


def _h(scope):
    return {"X-API-Key": KEYS[scope]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Seeded SQLite DB with one key per scope; returns a factory for apps."""
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for scope, raw in KEYS.items():
            s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(),
                         scope=scope, revoked_at=None))
        s.commit()
    yield url
    engine.dispose()


@pytest.fixture
def client(env):
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _boom_app(exc):
    """App with an extra /boom route raising `exc`."""
    app = create_app()

    @app.get("/boom")
    async def _boom():
        raise exc

    return app


def _trigger(client, status):
    if status == 401:
        return client.get(f"/v1/tasks/{UNKNOWN_ID}")
    if status == 403:
        return client.post("/v1/tasks", json={"name": "denied", "command": "echo hi"}, headers=_h("read"))
    if status == 404:
        return client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    if status == 409:
        body = {"name": "dup", "command": "echo hi"}
        client.post("/v1/tasks", json=body, headers=_h("write"))
        return client.post("/v1/tasks", json=body, headers=_h("write"))
    if status == 422:
        return client.post("/v1/tasks", json={"name": "empty-cmd", "command": ""}, headers=_h("write"))
    raise AssertionError(status)


def _assert_type(resp, status, type_):
    assert resp.status_code == status  # AC10.1-status
    assert resp.headers["content-type"].startswith(PROBLEM)  # AC10.5-problem-content-type
    assert resp.json()["type"] == type_  # AC10.5-problem-type


def _trigger_500(env):
    app = _boom_app(RuntimeError("SELECT * FROM tasks; Traceback (most recent call last): "
                                 "File /srv/app/x.py CREATE TABLE tasks sqlite"))
    return TestClient(app, raise_server_exceptions=False).get("/boom")


def test_fr10_status_code_and_type_mapping_for_each_error(env, tmp_path, monkeypatch):
    error_codes = "401,403,404,409,422,429,503,500".split(",")
    mapped = {}
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        for status in (401, 403, 404, 409, 422):
            r = _trigger(c, status)
            mapped[r.status_code] = r.json()["type"]
    monkeypatch.setenv("TASKQ_RATE_BURST", "3")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "0.01")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = [c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")) for _ in range(4)][-1]
        mapped[r.status_code] = r.json()["type"]
    empty = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/readyz")
        mapped[r.status_code] = r.json()["type"]
    empty.dispose()
    r = _trigger_500(env)
    mapped[r.status_code] = r.json()["type"]
    assert len(mapped) == 8  # AC10.5-mapping-size
    assert all(int(c) in mapped for c in error_codes)  # AC10.5-mapping-codes
    assert mapped == {401: "/errors/unauthenticated", 403: "/errors/forbidden", 404: "/errors/not-found",
                      409: "/errors/conflict", 422: "/errors/validation", 429: "/errors/rate-limited",
                      503: "/errors/not-ready", 500: "/errors/internal"}


def test_fr10_non_2xx_content_type_is_problem_json(client):
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    assert r.status_code == 404  # AC10.1-status
    assert r.headers["content-type"].startswith(PROBLEM)  # AC10.1-content-type


def test_fr10_problem_body_has_required_fields(client):
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    body = r.json()
    assert r.status_code == 404  # AC10.1-status
    assert all(f in body for f in REQUIRED_FIELDS)  # AC10.2-fields
    assert body["status"] == r.status_code  # AC10.2-body-status


def test_fr10_500_detail_has_no_sql_stack_path_or_schema(env):
    r = _trigger_500(env)
    assert r.status_code == 500  # AC10.1-status
    assert all(frag not in r.json()["detail"] for frag in FORBIDDEN_FRAGMENTS)  # AC10.3-no-internals


def test_fr10_status_401_type_unauthenticated(client):
    _assert_type(_trigger(client, 401), 401, "/errors/unauthenticated")


def test_fr10_status_403_type_forbidden(client):
    _assert_type(_trigger(client, 403), 403, "/errors/forbidden")


def test_fr10_status_404_type_not_found(client):
    _assert_type(_trigger(client, 404), 404, "/errors/not-found")


def test_fr10_status_409_type_conflict(client):
    _assert_type(_trigger(client, 409), 409, "/errors/conflict")


def test_fr10_status_422_type_validation(client):
    _assert_type(_trigger(client, 422), 422, "/errors/validation")


def test_fr10_status_429_type_rate_limited(env, monkeypatch):
    rate_burst, requests_sent = "3", "4"
    monkeypatch.setenv("TASKQ_RATE_BURST", rate_burst)
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "0.01")
    assert float(requests_sent) > int(rate_burst)  # AC10.5-over-burst
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        responses = [c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")) for _ in range(int(requests_sent))]
    _assert_type(responses[-1], 429, "/errors/rate-limited")
    assert "retry-after" in responses[-1].headers


def test_fr10_status_503_type_not_ready(tmp_path, monkeypatch):
    # Database without migrations/tables -> not ready.
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        _assert_type(c.get("/readyz"), 503, "/errors/not-ready")


def test_fr10_status_500_type_internal(env):
    _assert_type(_trigger_500(env), 500, "/errors/internal")


def test_fr10_correlation_id_in_header_and_server_log(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    header_value = r.headers.get("X-Correlation-Id", "")
    assert len(header_value) > 0  # AC10.4-header-present
    assert header_value == r.json()["correlation_id"]  # AC10.4-header-eq-body
    assert header_value in caplog.text  # AC10.4-in-log


def test_fr10_cancelled_error_not_converted_to_500(env):
    app = _boom_app(asyncio.CancelledError())
    sent = []

    async def call():
        scope = {"type": "http", "method": "GET", "path": "/boom", "raw_path": b"/boom",
                 "query_string": b"", "headers": [], "http_version": "1.1", "scheme": "http",
                 "server": ("test", 80), "client": ("test", 1), "root_path": "", "app": app}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    with pytest.raises(asyncio.CancelledError) as info:
        asyncio.run(call())
    assert info.type is asyncio.CancelledError  # AC10.6-type
    assert not any(m.get("status") == 500 for m in sent)  # AC10.6-not-500


def test_sec_t06_cors_default_denies_all_origins(env, monkeypatch):
    monkeypatch.delenv("TASKQ_CORS_ORIGINS", raising=False)
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/healthz", headers={"Origin": "https://evil.example"})
        pre = c.options("/v1/tasks", headers={"Origin": "https://evil.example",
                                              "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers  # T06-no-acao
    assert "access-control-allow-origin" not in pre.headers  # T06-no-acao


def test_sec_t14_500_body_has_no_internal_detail(env):
    r = _trigger_500(env)
    assert r.status_code == 500  # AC10.1-status
    assert all(frag not in r.json()["detail"] for frag in FORBIDDEN_FRAGMENTS)  # AC10.3-no-internals


def test_sec_t17_correlation_id_in_header_and_log(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    header_value = r.headers.get("X-Correlation-Id", "")
    assert len(header_value) > 0  # AC10.4-header-present
    assert header_value == r.json()["correlation_id"]  # AC10.4-header-eq-body
    assert header_value in caplog.text  # AC10.4-in-log
