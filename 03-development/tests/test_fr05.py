"""FR-05: per-key DB-backed token bucket, 429 problem+json with Retry-After,
health endpoints exempt, single-transaction row-locked updates.

All tests run in-process via TestClient against a per-test SQLite database.
"""
import hashlib
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from taskq_api.api import deps, middleware  # noqa: F401  (SAB: FR-05 modules)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.rate_bucket import RateBucket
from taskq_api.repository import rate_buckets
from taskq_api.service import rate_limit

KEY_A = "tq-fr05-key-a"
KEY_B = "tq-fr05-key-b"


def _add_key(session, raw, scope):
    key_id = str(uuid.uuid4())
    session.add(ApiKey(
        id=key_id,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scope=scope,
        revoked_at=None,
    ))
    return key_id


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Factory: build an app with the given burst / refill rate and seeded keys."""
    state = {}

    def _make(burst, per_sec):
        url = f"sqlite:///{tmp_path / 'taskq.db'}"
        monkeypatch.setenv("TASKQ_DB_URL", url)
        monkeypatch.setenv("TASKQ_RATE_BURST", str(burst))
        monkeypatch.setenv("TASKQ_RATE_PER_SEC", str(per_sec))
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            state["id_a"] = _add_key(s, KEY_A, "read")
            state["id_b"] = _add_key(s, KEY_B, "read")
            s.commit()
        state["engine"] = engine
        return create_app()

    _make.state = state
    yield _make
    if "engine" in state:
        state["engine"].dispose()


def _h(key):
    return {"X-API-Key": key}


def _bucket_rows(engine):
    with Session(engine) as s:
        return list(s.scalars(select(RateBucket)))


# --- AC-5.1: token bucket math ------------------------------------------------

def test_fr05_token_bucket_capacity_and_refill_rate_from_config():
    rate_burst, rate_per_sec, elapsed_seconds, tokens_before, expected_tokens_after = "20", "5.0", "2", "0", "10"
    result_tokens = rate_limit.refill(float(tokens_before), int(rate_burst), float(rate_per_sec),
                                      float(elapsed_seconds))
    assert float(rate_per_sec) * float(elapsed_seconds) == 10.0  # AC5.1-rate-math
    assert result_tokens == float(expected_tokens_after)  # AC5.1-refill
    assert result_tokens <= int(rate_burst)  # AC5.1-cap


def test_fr05_refill_never_exceeds_capacity():
    rate_burst, rate_per_sec, elapsed_seconds, tokens_before, expected_tokens_after = "20", "5.0", "10", "19", "20"
    assert float(tokens_before) + float(rate_per_sec) * float(elapsed_seconds) > int(rate_burst)  # AC5.1-overflow-input
    result_tokens = rate_limit.refill(float(tokens_before), int(rate_burst), float(rate_per_sec),
                                      float(elapsed_seconds))
    assert result_tokens == float(expected_tokens_after)  # AC5.1-refill
    assert result_tokens <= int(rate_burst)  # AC5.1-cap


# --- AC-5.2: 429 + problem+json + Retry-After ---------------------------------

def test_fr05_over_limit_returns_429_with_retry_after(make_app):
    rate_burst, rate_per_sec, requests_sent = "3", "0.01", "4"
    expected_status, expected_type = "429", "/errors/rate-limited"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    assert float(requests_sent) > int(rate_burst)  # AC5.2-over-burst
    responses = [client.get("/v1/tasks", headers=_h(KEY_A)) for _ in range(int(requests_sent))]
    assert all(r.status_code == 200 for r in responses[:int(rate_burst)])
    last = responses[-1]
    result_last_status = last.status_code
    assert result_last_status == int(expected_status)  # AC5.2-status
    assert last.headers["content-type"].startswith("application/problem+json")
    result_problem_type = last.json()["type"]
    assert result_problem_type == expected_type  # AC5.2-problem-type
    result_retry_after = last.headers["Retry-After"]
    assert float(result_retry_after) > 0  # AC5.2-retry-after


# --- AC-5.3: persisted state, single transaction ------------------------------

def test_fr05_bucket_state_persisted_in_db_with_row_lock_single_transaction(make_app):
    rate_burst, rate_per_sec, requests_sent, expected_tokens_row = "3", "0.01", "2", "1"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    engine = make_app.state["engine"]
    commits = []
    # count commits on the app's own engine: one per rate-limit update
    event.listen(client.app.state.engine, "commit", lambda conn: commits.append(1))
    for _ in range(int(requests_sent)):
        assert client.get("/v1/tasks", headers=_h(KEY_A)).status_code == 200
    rows = _bucket_rows(engine)
    result_bucket_row_count = len(rows)
    assert result_bucket_row_count == 1  # AC5.3-one-row
    assert rows[0].key_id == make_app.state["id_a"]
    result_tokens_persisted = round(rows[0].tokens)  # 3 - 2 + ~0 refill
    assert float(result_tokens_persisted) == int(expected_tokens_row)  # AC5.3-tokens
    result_update_transactions = sum(commits)
    assert result_update_transactions == int(requests_sent)  # AC5.3-tx-per-update


# --- AC-5.4: health exempt ----------------------------------------------------

def test_fr05_health_endpoints_not_rate_limited(make_app):
    health_path, rate_burst, rate_per_sec, requests_sent = "/healthz", "3", "0.01", "30"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    assert float(requests_sent) > int(rate_burst)  # AC5.4-over-burst
    result_non_200_count = sum(1 for _ in range(int(requests_sent)) if client.get(health_path).status_code != 200)
    assert result_non_200_count == 0  # AC5.4-health
    assert _bucket_rows(make_app.state["engine"]) == []


def test_fr05_readyz_not_rate_limited(make_app):
    health_path, rate_burst, rate_per_sec, requests_sent = "/readyz", "3", "0.01", "30"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    assert float(requests_sent) > int(rate_burst)  # AC5.4-over-burst
    result_non_200_count = sum(1 for _ in range(int(requests_sent)) if client.get(health_path).status_code != 200)
    assert result_non_200_count == 0  # AC5.4-health
    assert _bucket_rows(make_app.state["engine"]) == []


# --- AC-5.1: per-token isolation ----------------------------------------------

def test_fr05_per_token_isolation(make_app):
    rate_burst, rate_per_sec, requests_sent_key_a, expected_status_key_b = "3", "0.01", "4", "200"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    codes_a = [client.get("/v1/tasks", headers=_h(KEY_A)).status_code for _ in range(int(requests_sent_key_a))]
    result_status_code_key_a = codes_a[-1]
    assert result_status_code_key_a == 429  # AC5.1-key-a-limited
    result_status_code_key_b = client.get("/v1/tasks", headers=_h(KEY_B)).status_code
    assert result_status_code_key_b == int(expected_status_key_b)  # AC5.1-isolated


# --- NP-13: concurrency -------------------------------------------------------

def test_fr05_consume_token_state_transition_under_concurrent_load(make_app):
    rate_burst, rate_per_sec, concurrent_callers = "5", "0.0001", "20"
    make_app(int(rate_burst), float(rate_per_sec))
    engine = make_app.state["engine"]
    key_id = make_app.state["id_a"]
    assert float(concurrent_callers) > int(rate_burst)  # NP13-oversubscribed
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(int(concurrent_callers))

    def worker():
        barrier.wait()
        with Session(engine) as s:
            decision = rate_limit.consume(s, key_id, int(rate_burst), float(rate_per_sec))
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(int(concurrent_callers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == int(concurrent_callers)
    result_allowed_count = sum(1 for r in results if r)
    result_denied_count = sum(1 for r in results if not r)
    assert result_allowed_count == int(rate_burst)  # NP13-allowed
    assert result_denied_count == int(concurrent_callers) - int(rate_burst)  # NP13-denied
    assert len(_bucket_rows(engine)) == 1


# --- SEC T-05 -----------------------------------------------------------------

def test_sec_t05_burst_exceeded_returns_429(make_app):
    rate_burst, rate_per_sec, requests_sent, expected_status = "3", "0.01", "4", "429"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    assert float(requests_sent) > int(rate_burst)  # AC5.2-over-burst
    last = None
    for _ in range(int(requests_sent)):
        last = client.get("/v1/tasks", headers=_h(KEY_A))
    result_last_status = last.status_code
    assert result_last_status == int(expected_status)  # AC5.2-status


# --- NP-01 interaction --------------------------------------------------------

def test_fr05_unauthenticated_request_does_not_consume_token(make_app):
    rate_burst, rate_per_sec, requests_sent, expected_status = "3", "0.01", "5", "401"
    client = TestClient(make_app(int(rate_burst), float(rate_per_sec)))
    last = None
    for _ in range(int(requests_sent)):
        last = client.get("/v1/tasks")
    result_last_status = last.status_code
    assert result_last_status == int(expected_status)  # AC5.1-401-status
    result_bucket_row_count = len(_bucket_rows(make_app.state["engine"]))
    assert result_bucket_row_count == 0  # AC5.1-no-token-on-401


# --- coverage: first-request insert race (PostgreSQL path) ---------------------

def test_fr05_concurrent_first_request_falls_back_to_existing_row(make_app, monkeypatch):
    make_app(3, 0.01)
    engine = make_app.state["engine"]
    key_id = make_app.state["id_a"]
    with Session(engine) as s:
        assert rate_limit.consume(s, key_id, 3, 0.01).allowed  # creates the row
    real = rate_buckets.get_for_update
    calls = []

    def racy(session, kid):
        calls.append(1)
        return None if len(calls) == 1 else real(session, kid)  # first look misses the committed row

    monkeypatch.setattr(rate_buckets, "get_for_update", racy)
    with Session(engine) as s:
        assert rate_limit.consume(s, key_id, 3, 0.01).allowed
    assert len(calls) == 2
    assert len(_bucket_rows(engine)) == 1


def test_fr05_rate_config_rejects_non_positive(monkeypatch):
    monkeypatch.setenv("TASKQ_RATE_BURST", "0")
    with pytest.raises(ValueError):
        middleware.load_rate_config()


def test_fr05_insert_conflict_without_existing_row_reraises(make_app, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    make_app(3, 0.01)
    engine = make_app.state["engine"]
    key_id = make_app.state["id_a"]

    def conflict(session, bucket):
        raise IntegrityError("insert", {}, Exception("conflict"))

    monkeypatch.setattr(rate_buckets, "get_for_update", lambda session, kid: None)
    monkeypatch.setattr(rate_buckets, "add", conflict)
    with Session(engine) as s:
        with pytest.raises(IntegrityError):
            rate_limit.consume(s, key_id, 3, 0.01)
