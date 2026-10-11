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


def _trigger_500(env):
    app = _boom_app(RuntimeError("SELECT * FROM tasks; Traceback (most recent call last): "
                                 "File /srv/app/x.py CREATE TABLE tasks sqlite"))
    return TestClient(app, raise_server_exceptions=False).get("/boom")


def _typed(resp):
    return resp.status_code, resp.headers["content-type"], resp.json()["type"]


def test_fr10_status_code_and_type_mapping_for_each_error(env, tmp_path, monkeypatch):
    error_codes = "401,403,404,409,422,429,503,500"
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
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/readyz")
        mapped[r.status_code] = r.json()["type"]
    r = _trigger_500(env)
    mapped[r.status_code] = r.json()["type"]
    result_mapping_entries = mapped
    result_mapped_codes = [str(k) for k in mapped]
    assert len(result_mapping_entries) == 8  # AC10.5-mapping-size
    assert all(c in result_mapped_codes for c in error_codes.split(","))  # AC10.5-mapping-codes
    assert mapped == {401: "/errors/unauthenticated", 403: "/errors/forbidden", 404: "/errors/not-found",
                      409: "/errors/conflict", 422: "/errors/validation", 429: "/errors/rate-limited",
                      503: "/errors/not-ready", 500: "/errors/internal"}


def test_fr10_non_2xx_content_type_is_problem_json(client):
    expected_status, expected_content_type = "404", PROBLEM
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    result_status_code = r.status_code
    result_content_type = r.headers["content-type"]
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_content_type.startswith(expected_content_type)  # AC10.1-content-type


def test_fr10_problem_body_has_required_fields(client):
    expected_status = "404"
    required_fields = "type,title,status,detail,instance,correlation_id"
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    result_status_code = r.status_code
    result_body_keys = list(r.json().keys())
    result_body_status = r.json()["status"]
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert all(f in result_body_keys for f in required_fields.split(","))  # AC10.2-fields
    assert result_body_status == result_status_code  # AC10.2-body-status


def test_fr10_500_detail_has_no_sql_stack_path_or_schema(env):
    expected_status = "500"
    forbidden_fragments = "SELECT,Traceback,.py,sqlite,CREATE TABLE"
    r = _trigger_500(env)
    result_status_code = r.status_code
    result_detail = r.json()["detail"]
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert all(frag not in result_detail for frag in forbidden_fragments.split(","))  # AC10.3-no-internals


def test_fr10_status_401_type_unauthenticated(client):
    expected_status, expected_type = "401", "/errors/unauthenticated"
    r = _trigger(client, 401)
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_403_type_forbidden(client):
    expected_status, expected_type = "403", "/errors/forbidden"
    r = _trigger(client, 403)
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_404_type_not_found(client):
    expected_status, expected_type = "404", "/errors/not-found"
    r = _trigger(client, 404)
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_409_type_conflict(client):
    expected_status, expected_type = "409", "/errors/conflict"
    r = _trigger(client, 409)
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_422_type_validation(client):
    expected_status, expected_type = "422", "/errors/validation"
    r = _trigger(client, 422)
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_429_type_rate_limited(env, monkeypatch):
    rate_burst, requests_sent = "3", "4"
    expected_status, expected_type = "429", "/errors/rate-limited"
    monkeypatch.setenv("TASKQ_RATE_BURST", rate_burst)
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "0.01")
    assert float(requests_sent) > int(rate_burst)  # AC10.5-over-burst
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        responses = [c.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read")) for _ in range(int(requests_sent))]
    result_status_code, result_content_type, result_problem_type = _typed(responses[-1])
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type
    assert "retry-after" in responses[-1].headers


def test_fr10_status_503_type_not_ready(tmp_path, monkeypatch):
    expected_status, expected_type = "503", "/errors/not-ready"
    # Database without migrations/tables -> not ready.
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/readyz")
    result_status_code, result_content_type, result_problem_type = _typed(r)
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def test_fr10_status_500_type_internal(env):
    expected_status, expected_type = "500", "/errors/internal"
    result_status_code, result_content_type, result_problem_type = _typed(_trigger_500(env))
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert result_problem_type == expected_type  # AC10.5-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC10.5-problem-content-type


def _corr(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_h("read"))
    return r.headers.get("X-Correlation-Id", ""), r.json()["correlation_id"], caplog.text


def test_fr10_correlation_id_in_header_and_server_log(client, caplog):
    result_header_value, result_body_correlation_id, result_log_text = _corr(client, caplog)
    assert len(result_header_value) > 0  # AC10.4-header-present
    assert result_header_value == result_body_correlation_id  # AC10.4-header-eq-body
    assert result_header_value in result_log_text  # AC10.4-in-log


def test_fr10_cancelled_error_not_converted_to_500(env):
    expected_exception_type = asyncio.CancelledError
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
    result_exception_type = info.type
    result_converted_to_500 = any(m.get("status") == 500 for m in sent)
    assert result_exception_type == expected_exception_type  # AC10.6-type
    assert result_converted_to_500 == False  # noqa: E712  # AC10.6-not-500


def test_sec_t06_cors_default_denies_all_origins(env, monkeypatch):
    forbidden_header = "access-control-allow-origin"
    monkeypatch.delenv("TASKQ_CORS_ORIGINS", raising=False)
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/healthz", headers={"Origin": "https://evil.example"})
        pre = c.options("/v1/tasks", headers={"Origin": "https://evil.example",
                                              "Access-Control-Request-Method": "GET"})
    result_response_headers = {**r.headers, **pre.headers}
    assert forbidden_header not in result_response_headers  # T06-no-acao


def test_sec_t14_500_body_has_no_internal_detail(env):
    expected_status = "500"
    forbidden_fragments = "SELECT,Traceback,.py,sqlite,CREATE TABLE"
    r = _trigger_500(env)
    result_status_code = r.status_code
    result_detail = r.json()["detail"]
    assert result_status_code == int(expected_status)  # AC10.1-status
    assert all(frag not in result_detail for frag in forbidden_fragments.split(","))  # AC10.3-no-internals


def test_sec_t17_correlation_id_in_header_and_log(client, caplog):
    result_header_value, result_body_correlation_id, result_log_text = _corr(client, caplog)
    assert len(result_header_value) > 0  # AC10.4-header-present
    assert result_header_value == result_body_correlation_id  # AC10.4-header-eq-body
    assert result_header_value in result_log_text  # AC10.4-in-log
