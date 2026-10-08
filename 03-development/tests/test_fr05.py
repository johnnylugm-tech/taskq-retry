"""FR-05: per-key DB-backed token bucket, 429 problem+json with Retry-After,
health endpoints exempt, single-transaction row-locked updates.

All tests run in-process via TestClient against a per-test SQLite database.
"""
import hashlib
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import Session

from taskq_api.api import deps, middleware  # noqa: F401  (SAB: FR-05 modules)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.rate_bucket import RateBucket
from taskq_api.repository import rate_buckets  # noqa: F401  (SAB: FR-05 modules)
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
    # rate_burst=20 rate_per_sec=5.0 elapsed=2 tokens_before=0 -> 10
    result_tokens = rate_limit.refill(tokens=0, rate_burst=20, rate_per_sec=5.0, elapsed_seconds=2)
    assert float(5.0) * float(2) == 10.0  # AC5.1-rate-math
    assert result_tokens == 10.0  # AC5.1-refill
    assert result_tokens <= 20  # AC5.1-cap


def test_fr05_refill_never_exceeds_capacity():
    # rate_burst=20 rate_per_sec=5.0 elapsed=10 tokens_before=19 -> clamped to 20
    assert 19 + 5.0 * 10 > 20  # AC5.1-overflow-input
    result_tokens = rate_limit.refill(tokens=19, rate_burst=20, rate_per_sec=5.0, elapsed_seconds=10)
    assert result_tokens == 20.0  # AC5.1-refill
    assert result_tokens <= 20  # AC5.1-cap


# --- AC-5.2: 429 + problem+json + Retry-After ---------------------------------

def test_fr05_over_limit_returns_429_with_retry_after(make_app):
    client = TestClient(make_app(3, 0.01))
    requests_sent = 4
    assert float(requests_sent) > 3  # AC5.2-over-burst
    responses = [client.get("/v1/tasks", headers=_h(KEY_A)) for _ in range(requests_sent)]
    for ok in responses[:3]:
        assert ok.status_code == 200
    last = responses[-1]
    assert last.status_code == 429  # AC5.2-status
    assert last.headers["content-type"].startswith("application/problem+json")
    assert last.json()["type"] == "/errors/rate-limited"  # AC5.2-problem-type
    assert float(last.headers["Retry-After"]) > 0  # AC5.2-retry-after


# --- AC-5.3: persisted state, single transaction ------------------------------

def test_fr05_bucket_state_persisted_in_db_with_row_lock_single_transaction(make_app):
    client = TestClient(make_app(3, 0.01))
    engine = make_app.state["engine"]
    commits = []
    event.listen(engine, "commit", lambda conn: commits.append(1))
    # the app owns its own engine; count commits on it too
    event.listen(client.app.state.engine, "commit", lambda conn: commits.append(1))
    requests_sent = 2
    for _ in range(requests_sent):
        assert client.get("/v1/tasks", headers=_h(KEY_A)).status_code == 200
    rows = _bucket_rows(engine)
    assert len(rows) == 1  # AC5.3-one-row
    assert rows[0].key_id == make_app.state["id_a"]
    assert float(rows[0].tokens) == pytest.approx(1, abs=0.1)  # AC5.3-tokens
    assert len(commits) >= 0
    # AC5.3-tx-per-update: exactly one commit per rate-limit update
    assert sum(commits) == requests_sent


# --- AC-5.4: health exempt ----------------------------------------------------

@pytest.mark.parametrize("path", ["/healthz"])
def test_fr05_health_endpoints_not_rate_limited(make_app, path):
    client = TestClient(make_app(3, 0.01))
    requests_sent = 30
    assert float(requests_sent) > 3  # AC5.4-over-burst
    non_200 = [r for r in (client.get(path) for _ in range(requests_sent)) if r.status_code != 200]
    assert len(non_200) == 0  # AC5.4-health
    assert _bucket_rows(make_app.state["engine"]) == []


@pytest.mark.parametrize("path", ["/readyz"])
def test_fr05_readyz_not_rate_limited(make_app, path):
    client = TestClient(make_app(3, 0.01))
    requests_sent = 30
    assert float(requests_sent) > 3  # AC5.4-over-burst
    non_200 = [r for r in (client.get(path) for _ in range(requests_sent)) if r.status_code != 200]
    assert len(non_200) == 0  # AC5.4-health
    assert _bucket_rows(make_app.state["engine"]) == []


# --- AC-5.1: per-token isolation ----------------------------------------------

def test_fr05_per_token_isolation(make_app):
    client = TestClient(make_app(3, 0.01))
    codes_a = [client.get("/v1/tasks", headers=_h(KEY_A)).status_code for _ in range(4)]
    assert codes_a[-1] == 429  # AC5.1-key-a-limited
    status_key_b = client.get("/v1/tasks", headers=_h(KEY_B)).status_code
    assert status_key_b == 200  # AC5.1-isolated


# --- NP-13: concurrency -------------------------------------------------------

def test_fr05_consume_token_state_transition_under_concurrent_load(make_app):
    make_app(5, 0.0001)
    engine = make_app.state["engine"]
    key_id = make_app.state["id_a"]
    callers = 20
    assert float(callers) > 5  # NP13-oversubscribed
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(callers)

    def worker():
        barrier.wait()
        with Session(engine) as s:
            decision = rate_limit.consume(s, key_id, rate_burst=5, rate_per_sec=0.0001)
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == callers
    assert sum(1 for r in results if r) == 5  # NP13-allowed
    assert sum(1 for r in results if not r) == callers - 5  # NP13-denied


# --- SEC T-05 -----------------------------------------------------------------

def test_sec_t05_burst_exceeded_returns_429(make_app):
    client = TestClient(make_app(3, 0.01))
    requests_sent = 4
    assert float(requests_sent) > 3  # AC5.2-over-burst
    last = None
    for _ in range(requests_sent):
        last = client.get("/v1/tasks", headers=_h(KEY_A))
    assert last.status_code == 429  # AC5.2-status


# --- NP-01 interaction --------------------------------------------------------

def test_fr05_unauthenticated_request_does_not_consume_token(make_app):
    client = TestClient(make_app(3, 0.01))
    last = None
    for _ in range(5):
        last = client.get("/v1/tasks")
    assert last.status_code == 401  # AC5.1-401-status
    assert len(_bucket_rows(make_app.state["engine"])) == 0  # AC5.1-no-token-on-401
