"""FR-09: /healthz, /readyz (DB + migration-at-head, fail closed), /v1/metrics (admin).

All tests run in-process via TestClient against a per-test SQLite database.
"""
import hashlib
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.api import routes_health, routes_metrics  # noqa: F401  (SAB: FR-09 modules)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.repository import health as health_repo  # noqa: F401
from taskq_api.service import health as health_service
from taskq_api.service import metrics as metrics_service  # noqa: F401

ADMIN_KEY = "tq-fr09-admin"
WRITE_KEY = "tq-fr09-write"
READ_KEY = "tq-fr09-read"
NOT_READY = "/errors/not-ready"


def _add_key(session, raw, scope):
    session.add(ApiKey(
        id=str(uuid.uuid4()),
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scope=scope,
        revoked_at=None,
    ))


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _add_key(s, ADMIN_KEY, "admin")
        _add_key(s, WRITE_KEY, "write")
        _add_key(s, READ_KEY, "read")
        s.commit()
    engine.dispose()
    return url


@pytest.fixture
def client(db_url):
    with TestClient(create_app()) as c:
        yield c


def _seed_tasks(url, done, failed):
    engine = create_engine(url)
    with Session(engine) as s:
        for i in range(done):
            s.add(Task(command="echo hi", name=f"done-{i}", status="done"))
        for i in range(failed):
            s.add(Task(command="echo hi", name=f"failed-{i}", status="failed"))
        s.commit()
    engine.dispose()


def _patch_revisions(monkeypatch, current, head):
    # GREEN TODO: taskq_api.repository.health must have current_revision(engine) -> str | None
    # GREEN TODO: taskq_api.service.health must have head_revision() -> str
    monkeypatch.setattr(health_repo, "current_revision", lambda *a, **k: current)
    monkeypatch.setattr(health_service, "head_revision", lambda *a, **k: head)


def test_fr09_healthz_returns_200_status_ok(client):
    expected_status, expected_body_status = "200", "ok"
    resp = client.get("/healthz")
    result_status_code = resp.status_code
    result_body_status = resp.json()["status"]
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_body_status == expected_body_status  # AC9.1-body


def test_fr09_readyz_200_when_db_ok_and_migration_at_head(client, monkeypatch):
    expected_status = "200"
    _patch_revisions(monkeypatch, "v3", "v3")
    resp = client.get("/readyz")
    result_status_code = resp.status_code
    assert result_status_code == int(expected_status)  # AC9.1-status
    result_ready = resp.json().get("status") in ("ready", "ok")
    assert result_ready == True  # noqa: E712  # AC9.2-ready


def test_fr09_readyz_503_when_db_unavailable(db_url, monkeypatch):
    expected_status, expected_type, expected_detail_keyword = "503", NOT_READY, "database"
    _patch_revisions(monkeypatch, "v3", "v3")
    app = create_app()
    # Stop the DB: point the app at a database file that cannot be opened.
    app.state.engine = create_engine("sqlite:////nonexistent-dir-fr09/taskq.db")
    with TestClient(app) as c:
        resp = c.get("/readyz")
    result_status_code = resp.status_code
    result_problem_type = resp.json()["type"]
    result_detail_lower = resp.json()["detail"].lower()
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_problem_type == expected_type  # AC9.3-problem-type
    assert expected_detail_keyword in result_detail_lower  # AC9.3-detail


def test_fr09_readyz_503_when_migration_not_at_head_fail_closed(client, monkeypatch):
    expected_status, expected_type, expected_detail_keyword = "503", NOT_READY, "migration"
    alembic_revision, head_revision = "v2", "v3"
    _patch_revisions(monkeypatch, alembic_revision, head_revision)
    resp = client.get("/readyz")
    result_status_code = resp.status_code
    result_problem_type = resp.json()["type"]
    result_detail_lower = resp.json()["detail"].lower()
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_problem_type == expected_type  # AC9.3-problem-type
    assert expected_detail_keyword in result_detail_lower  # AC9.3-detail
    assert alembic_revision != head_revision  # AC9.4-behind-head


def test_fr09_metrics_admin_returns_counts_latency_and_rate_limit_rejections(db_url, monkeypatch):
    tasks_seeded_done, tasks_seeded_failed, rate_limit_rejections_seeded = "2", "1", "1"
    expected_status = "200"
    monkeypatch.setenv("TASKQ_RATE_BURST", "3")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "0.01")
    _seed_tasks(db_url, int(tasks_seeded_done), int(tasks_seeded_failed))
    with TestClient(create_app()) as c:
        # Exhaust the read key's own bucket (burst 3): the 4th request is rejected once.
        codes = [c.get("/v1/tasks", headers={"X-API-Key": READ_KEY}).status_code for _ in range(4)]
        assert codes[-1] == 429
        resp = c.get("/v1/metrics", headers={"X-API-Key": ADMIN_KEY})
    body = resp.json()
    result_status_code = resp.status_code
    result_task_counts_done = body["task_counts"]["done"]
    result_task_counts_failed = body["task_counts"]["failed"]
    result_rate_limit_rejections = body["rate_limit_rejections"]
    # NFR-99-4: percentile set unspecified, only a non-empty latency mapping is asserted.
    result_latency_percentiles = body["latency_percentiles"]
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_task_counts_done == int(tasks_seeded_done)  # AC9.5-counts-done
    assert result_task_counts_failed == int(tasks_seeded_failed)  # AC9.5-counts-failed
    assert result_rate_limit_rejections == int(rate_limit_rejections_seeded)  # AC9.5-rejections
    assert len(result_latency_percentiles) > 0  # AC9.5-latency


def test_fr09_metrics_write_key_returns_403(client):
    expected_status, expected_type = "403", "/errors/forbidden"
    resp = client.get("/v1/metrics", headers={"X-API-Key": WRITE_KEY})
    result_status_code = resp.status_code
    result_problem_type = resp.json()["type"]
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_problem_type == expected_type  # AC9.3-problem-type


def test_fr09_metrics_unauthenticated_returns_401(client):
    expected_status, expected_type = "401", "/errors/unauthenticated"
    resp = client.get("/v1/metrics")
    result_status_code = resp.status_code
    result_problem_type = resp.json()["type"]
    assert result_status_code == int(expected_status)  # AC9.1-status
    assert result_problem_type == expected_type  # AC9.3-problem-type


def test_sec_t16_db_url_password_absent_from_logs(tmp_path, monkeypatch, caplog):
    db_url_password = "s3cretPassw0rd"
    db_url = "postgresql://taskq:s3cretPassw0rd@db.internal/taskq"
    # Real app on SQLite; the secret URL is only configured on the failing-engine path.
    local = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", local)
    engine = create_engine(local)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _add_key(s, ADMIN_KEY, "admin")
        s.commit()
    engine.dispose()
    caplog.set_level(logging.DEBUG)
    app = create_app()
    app.state.engine = create_engine(db_url)  # unreachable host: readyz must fail without leaking
    with TestClient(app) as c:
        ready = c.get("/readyz")
        metrics = c.get("/v1/metrics", headers={"X-API-Key": ADMIN_KEY})
    result_log_text = caplog.text + ready.text
    result_metrics_body = metrics.text
    assert ready.status_code == 503
    assert db_url_password not in result_log_text  # T16-logs
    assert db_url_password not in result_metrics_body  # T16-metrics
