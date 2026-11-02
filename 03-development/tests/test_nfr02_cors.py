"""NFR-02: CORS denies every origin unless TASKQ_CORS_ORIGINS lists it.

Runs in-process via TestClient against a per-test SQLite database. The two
declared TEST_SPEC cases (NFR-02 #4, #5) come first; the rest pin the parts of
the allowlist behaviour the declaration leaves open.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from taskq_api.api import middleware  # noqa: F401  (SAB: NFR-02 module)
from taskq_api.app import create_app
from taskq_api.models.base import Base

ALLOWED = "https://app.example.com"
OTHER = "https://other.example.org"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Empty SQLite DB with the schema, so the app lifespan can start."""
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _get(origin):
    with TestClient(create_app()) as c:
        return c.get("/healthz", headers={"Origin": origin})


def _preflight(origin, headers="X-API-Key"):
    with TestClient(create_app()) as c:
        return c.options("/v1/tasks", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": headers,
        })


def test_nfr02_cors_denies_all_origins_by_default_and_honours_allowlist(env, monkeypatch):
    origin, cors_origins_env = "https://evil.example", ""
    forbidden_header = "access-control-allow-origin"
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", cors_origins_env)
    simple = _get(origin)
    preflight = _preflight(origin)
    result_response_headers = {**simple.headers, **preflight.headers}
    assert forbidden_header not in result_response_headers  # N2.6-default-deny
    # An allowlist that does not name the origin keeps it denied.
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", ALLOWED)
    assert forbidden_header not in _get(origin).headers


def test_nfr02_cors_allowlisted_origin_receives_allow_header(env, monkeypatch):
    origin, cors_origins_env = ALLOWED, ALLOWED
    expected_header = "access-control-allow-origin"
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", cors_origins_env)
    r = _get(origin)
    result_allow_origin = r.headers.get(expected_header)
    assert result_allow_origin == origin  # N2.6-allowlist


def test_nfr02_cors_unset_variable_is_deny_all(env, monkeypatch):
    monkeypatch.delenv("TASKQ_CORS_ORIGINS", raising=False)
    assert "access-control-allow-origin" not in _get(ALLOWED).headers


def test_nfr02_cors_every_listed_origin_is_allowed_and_only_those(env, monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", f" {ALLOWED} , {OTHER},,")
    assert _get(ALLOWED).headers["access-control-allow-origin"] == ALLOWED
    assert _get(OTHER).headers["access-control-allow-origin"] == OTHER
    assert "access-control-allow-origin" not in _get("https://third.example").headers


def test_nfr02_cors_preflight_for_a_listed_origin_allows_the_api_key_header(env, monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", ALLOWED)
    r = _preflight(ALLOWED)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "x-api-key" in r.headers["access-control-allow-headers"].lower()
    assert "POST" in r.headers["access-control-allow-methods"]


def test_nfr02_cors_preflight_for_an_unlisted_origin_is_rejected(env, monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", ALLOWED)
    r = _preflight(OTHER)
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_nfr02_cors_exposes_the_contract_headers_and_never_credentials(env, monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", ALLOWED)
    r = _get(ALLOWED)
    exposed = r.headers["access-control-expose-headers"].lower()
    assert "x-correlation-id" in exposed and "retry-after" in exposed
    assert "access-control-allow-credentials" not in r.headers


def test_nfr02_cors_wildcard_is_refused_at_startup(env, monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="TASKQ_CORS_ORIGINS"):
        create_app()
