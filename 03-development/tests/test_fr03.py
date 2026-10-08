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
    expected_status = "401"
    expected_type = PROBLEM_TYPE
    result_status_code = resp.status_code
    result_content_type = resp.headers["content-type"]
    result_problem_type = resp.json()["type"]
    assert result_status_code == int(expected_status)  # AC3.1-status
    assert result_content_type.startswith("application/problem+json")  # AC3.1-content-type
    assert result_problem_type == expected_type  # AC3.1-problem-type


def test_fr03_missing_or_invalid_api_key_returns_401_problem_json(client):
    resp = client.get("/v1/tasks")
    expected_status = "401"
    expected_type = PROBLEM_TYPE
    result_status_code = resp.status_code
    result_content_type = resp.headers["content-type"]
    result_problem_type = resp.json()["type"]
    assert result_status_code == int(expected_status)  # AC3.1-status
    assert result_problem_type == expected_type  # AC3.1-problem-type
    assert result_content_type.startswith("application/problem+json")  # AC3.1-content-type


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
    result_compare_function = "hmac.compare_digest" if calls else "other"
    expected_compare_function = "hmac.compare_digest"
    assert result_compare_function == expected_compare_function  # AC3.2-compare
    engine = create_engine(db_url)
    with Session(engine) as s:
        stored = s.scalar(select(ApiKey.key_hash))
    engine.dispose()
    result_key_hash = stored
    plaintext_key = GOOD_KEY
    assert len(result_key_hash) == 64  # AC3.2-hash-length
    assert result_key_hash == hashlib.sha256(GOOD_KEY.encode()).hexdigest()
    assert plaintext_key != result_key_hash  # AC3.2-no-plaintext


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
    result_exit_code = proc.returncode
    assert result_exit_code == 0  # AC3.3-exit
    tokens = proc.stdout.split()
    engine = create_engine(url)
    with Session(engine) as s:
        row = s.scalars(select(ApiKey)).one()
    engine.dispose()
    result_key_scope = row.scope
    expected_scope = "write"
    assert result_key_scope == expected_scope  # AC3.3-scope
    # the printed plaintext is the token whose sha256 equals the stored hash
    matches = [t for t in tokens if hashlib.sha256(t.encode()).hexdigest() == row.key_hash]
    result_stdout_plaintext_count = proc.stdout.count(matches[0]) if matches else 0
    assert len(matches) == 1
    assert result_stdout_plaintext_count == 1  # AC3.3-once
    assert matches[0] not in db_file.read_bytes().decode("latin-1")


def test_fr03_revoked_key_is_rejected(client, db_url):
    _seed(db_url, GOOD_KEY, scope="read", revoked=True)
    resp = client.get("/v1/tasks", headers={"X-API-Key": GOOD_KEY})
    result_key_accepted = resp.status_code == 200
    assert result_key_accepted == False  # AC3.4-revoked  # noqa: E712
    _assert_401_problem(resp)


def test_fr03_health_endpoints_do_not_require_auth(client):
    expected_status = "200"
    result_status_code = client.get("/healthz").status_code
    assert result_status_code == int(expected_status)  # AC3.5-health-body


def test_fr03_readyz_does_not_require_auth(client):
    expected_status = "200"
    result_status_code = client.get("/readyz").status_code
    assert result_status_code == int(expected_status)  # AC3.5-health-body


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
    result_key_hash = stored
    plaintext_key = plaintext
    assert len(result_key_hash) == 64  # AC3.2-hash-length
    assert plaintext_key != result_key_hash  # AC3.2-no-plaintext
    db_file = Path(db_url.removeprefix("sqlite:///"))
    result_plaintext_occurrences_in_db = db_file.read_bytes().count(plaintext.encode())
    assert result_plaintext_occurrences_in_db == 0  # T08-no-plaintext-in-db
