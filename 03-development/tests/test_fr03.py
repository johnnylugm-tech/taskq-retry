"""FR-03: API key authentication (X-API-Key, SHA-256 at rest, compare_digest, revocation).

CLI test runs out-of-process (real user entry point) with a shared TASKQ_DB_URL and
PYTHONPATH propagated; everything else is in-process via TestClient.
"""
import hashlib
import hmac
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from taskq_api import __main__ as cli_main  # noqa: F401  (SAB: FR-03 modules)
from taskq_api.api import deps  # noqa: F401
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.repository import api_keys  # noqa: F401
from taskq_api.service import auth as auth_service
from taskq_api.service import keys as key_service  # noqa: F401

PROBLEM_TYPE = "/errors/unauthenticated"
GOOD_KEY = "tq-example-plaintext-key-0001"


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _seed(url, plaintext, scope="read", revoked=False):
    engine = create_engine(url)
    with Session(engine) as s:
        s.add(ApiKey(
            id=str(uuid.uuid4()),
            key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
            scope=scope,
            revoked_at=datetime(2026, 1, 1) if revoked else None,
        ))
        s.commit()
    engine.dispose()


@pytest.fixture
def client(db_url):
    with TestClient(create_app()) as c:
        yield c


def _assert_401_problem(resp):
    assert resp.status_code == 401  # AC3.1-status
    assert resp.headers["content-type"].startswith("application/problem+json")  # AC3.1-content-type
    assert resp.json()["type"] == PROBLEM_TYPE  # AC3.1-problem-type


def test_fr03_missing_or_invalid_api_key_returns_401_problem_json(client):
    _assert_401_problem(client.get("/v1/tasks"))


def test_fr03_unknown_api_key_value_returns_401(client, db_url):
    _seed(db_url, GOOD_KEY)
    _assert_401_problem(client.get("/v1/tasks", headers={"X-API-Key": "not-a-real-key"}))


def test_fr03_empty_api_key_header_returns_401(client):
    _assert_401_problem(client.get("/v1/tasks", headers={"X-API-Key": ""}))


# GREEN TODO: taskq_api.service.auth must `import hmac` and call hmac.compare_digest(...)
# when verifying a presented key against the stored hash.
def test_fr03_key_stored_as_sha256_and_compared_with_compare_digest(client, db_url, monkeypatch):
    _seed(db_url, GOOD_KEY)
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_service.hmac, "compare_digest", spy)
    resp = client.get("/v1/tasks", headers={"X-API-Key": GOOD_KEY})
    assert resp.status_code == 200
    assert calls, "hmac.compare_digest was not used"  # AC3.2-compare
    engine = create_engine(db_url)
    with Session(engine) as s:
        stored = s.scalar(select(ApiKey.key_hash))
    engine.dispose()
    assert len(stored) == 64  # AC3.2-hash-length
    assert stored == hashlib.sha256(GOOD_KEY.encode()).hexdigest()
    assert GOOD_KEY != stored  # AC3.2-no-plaintext


def test_fr03_key_create_prints_plaintext_once_with_scope(tmp_path):
    db_file = tmp_path / "taskq.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    env = os.environ.copy()
    env["TASKQ_DB_URL"] = url
    src_root = Path(__file__).resolve().parent.parent / "src"
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "taskq_api", "key", "create", "--scope", "write"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0  # AC3.3-exit
    tokens = proc.stdout.split()
    engine = create_engine(url)
    with Session(engine) as s:
        row = s.scalars(select(ApiKey)).one()
    engine.dispose()
    assert row.scope == "write"  # AC3.3-scope
    # the printed plaintext is the token whose sha256 equals the stored hash
    matches = [t for t in tokens if hashlib.sha256(t.encode()).hexdigest() == row.key_hash]
    assert len(matches) == 1  # AC3.3-once
    assert proc.stdout.count(matches[0]) == 1
    assert matches[0] not in db_file.read_bytes().decode("latin-1")


def test_fr03_revoked_key_is_rejected(client, db_url):
    _seed(db_url, GOOD_KEY, scope="read", revoked=True)
    _assert_401_problem(client.get("/v1/tasks", headers={"X-API-Key": GOOD_KEY}))  # AC3.4-revoked


def test_fr03_health_endpoints_do_not_require_auth(client):
    assert client.get("/healthz").status_code == 200  # AC3.5-health-body


def test_fr03_readyz_does_not_require_auth(client):
    assert client.get("/readyz").status_code == 200  # AC3.5-health-body


def test_sec_t01_invalid_api_key_rejected(client):
    _assert_401_problem(client.post(
        "/v1/tasks", json={"name": "x", "command": "echo hi"}, headers={"X-API-Key": "forged-key-123"},
    ))


def test_sec_t02_revoked_key_rejected(client, db_url):
    _seed(db_url, GOOD_KEY, scope="admin", revoked=True)
    _assert_401_problem(client.get("/v1/tasks", headers={"X-API-Key": GOOD_KEY}))


# GREEN TODO: taskq_api.service.auth must `import hmac` and call hmac.compare_digest(...).
def test_sec_t03_key_compare_uses_compare_digest(client, db_url, monkeypatch):
    plaintext = "tq-example-plaintext-key-0002"
    _seed(db_url, plaintext)
    seen = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append(True)
        return real(a, b)

    monkeypatch.setattr(auth_service.hmac, "compare_digest", spy)
    client.get("/v1/tasks", headers={"X-API-Key": plaintext})
    assert seen  # AC3.2-compare


def test_sec_t08_key_stored_as_sha256_only(db_url):
    plaintext = "tq-example-plaintext-key-0003"
    _seed(db_url, plaintext)
    engine = create_engine(db_url)
    with Session(engine) as s:
        stored = s.scalar(select(ApiKey.key_hash))
    engine.dispose()
    assert len(stored) == 64  # AC3.2-hash-length
    assert plaintext != stored  # AC3.2-no-plaintext
    db_file = Path(db_url.removeprefix("sqlite:///"))
    assert plaintext.encode() not in db_file.read_bytes()  # T08-no-plaintext-in-db
