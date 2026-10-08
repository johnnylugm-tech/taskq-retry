"""FR-01: task resource CRUD API (POST/GET/GET list/DELETE /v1/tasks)."""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from taskq_api.api import routes_tasks, schemas  # noqa: F401  (SAB: FR-01 modules)
from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.models.task_result import TaskResult
from taskq_api.repository import tasks as repo_tasks  # noqa: F401
from taskq_api.service import tasks as svc_tasks  # noqa: F401
from taskq_api.service import validation  # noqa: F401

KEYS = {
    "read": "read-key-0000000000000000",
    "write": "write-key-000000000000000",
    "admin": "admin-key-00000000000000000",
}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


def _hdr(scope):
    return {"X-API-Key": KEYS[scope]}


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for scope, raw in KEYS.items():
            s.add(ApiKey(id=str(uuid.uuid4()), key_hash=hashlib.sha256(raw.encode()).hexdigest(), scope=scope))
        s.commit()
    engine.dispose()
    return url


@pytest.fixture
def client(db_url):
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db(db_url):
    engine = create_engine(db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _post(client, name, command="echo hello", scope="write"):
    return client.post("/v1/tasks", json={"name": name, "command": command}, headers=_hdr(scope))


def _seed(client, n, prefix="t"):
    ids = []
    for i in range(n):
        r = _post(client, f"{prefix}-{i:04d}")
        assert r.status_code == 201
        ids.append(r.json()["id"])
    return ids


def _assert_problem(resp, status, type_suffix):
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"].endswith(type_suffix)
    assert body["status"] == status


def test_fr01_post_task_write_key_returns_201_with_id(client):
    r = _post(client, "build-docs")
    assert r.status_code == 201
    assert r.json()["id"]


def test_fr01_get_task_read_key_returns_all_fields(client):
    task_id = _post(client, "build-docs").json()["id"]
    r = client.get(f"/v1/tasks/{task_id}", headers=_hdr("read"))
    assert r.status_code == 200
    body = r.json()
    for field in ("id", "command", "name", "status", "created_at"):
        assert field in body
    assert body["id"] == task_id
    assert body["name"] == "build-docs"
    assert body["command"] == "echo hello"


def test_fr01_list_tasks_supports_status_limit_cursor(client):
    _seed(client, 5)
    seen, cursor = [], None
    for _ in range(3):
        params = {"status": "pending", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/v1/tasks", params=params, headers=_hdr("read"))
        assert r.status_code == 200
        body = r.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body.get("next_cursor")
        if len(seen) < 5:
            assert len(body["items"]) == 2
            assert cursor
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_fr01_delete_task_admin_removes_task_and_results_in_one_transaction(client, db):
    task_id = _post(client, "doomed").json()["id"]
    for i in range(2):
        db.add(TaskResult(id=str(uuid.uuid4()), task_id=task_id, exit_code=i, stdout_tail="", stderr_tail="", duration_ms=1))
    db.commit()
    r = client.delete(f"/v1/tasks/{task_id}", headers=_hdr("admin"))
    assert 200 <= r.status_code < 300
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Task).where(Task.id == task_id)) == 0
    assert db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task_id)) == 0
    assert client.get(f"/v1/tasks/{task_id}", headers=_hdr("read")).status_code == 404


def test_fr01_invalid_body_returns_422_problem_json(client):
    _assert_problem(_post(client, "empty-cmd", command=""), 422, "/errors/validation")


def test_fr01_empty_name_rejected_422(client):
    _assert_problem(_post(client, "", command="echo hi"), 422, "/errors/validation")


def test_fr01_command_at_1000_chars_accepted(client):
    assert _post(client, "max-len", command="a" * 1000).status_code == 201


def test_fr01_command_above_1000_chars_rejected_422(client):
    _assert_problem(_post(client, "too-long", command="a" * 1001), 422, "/errors/validation")


def test_fr01_unknown_id_returns_404_problem_json(client):
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_hdr("read"))
    _assert_problem(r, 404, "/errors/not-found")


def test_fr01_pagination_is_cursor_based_not_offset(client):
    ids = _seed(client, 25)
    r = client.get("/v1/tasks", params={"limit": 10}, headers=_hdr("read"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 10
    assert body["next_cursor"]
    # offset is not a supported pagination mechanism: it must not change the page
    r_off = client.get("/v1/tasks", params={"limit": 10, "offset": 10}, headers=_hdr("read"))
    assert [i["id"] for i in r_off.json()["items"]] == [i["id"] for i in body["items"]]
    # the cursor advances to the next, non-overlapping page
    r2 = client.get("/v1/tasks", params={"limit": 10, "cursor": body["next_cursor"]}, headers=_hdr("read"))
    assert r2.status_code == 200
    page1 = {i["id"] for i in body["items"]}
    page2 = {i["id"] for i in r2.json()["items"]}
    assert len(page2) == 10
    assert not page1 & page2
    assert (page1 | page2) <= set(ids)


def test_fr01_list_default_limit_50_max_200_over_limit_422(client):
    _seed(client, 60)
    r = client.get("/v1/tasks", headers=_hdr("read"))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 50


def test_fr01_list_limit_200_accepted(client):
    _seed(client, 250)
    r = client.get("/v1/tasks", params={"limit": 200}, headers=_hdr("read"))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 200


def test_fr01_list_limit_201_rejected_422(client):
    r = client.get("/v1/tasks", params={"limit": 201}, headers=_hdr("read"))
    _assert_problem(r, 422, "/errors/validation")


def test_fr01_duplicate_name_returns_409(client):
    assert _post(client, "build-docs").status_code == 201
    _assert_problem(_post(client, "build-docs"), 409, "/errors/conflict")


def test_sec_t07_sql_injection_payload_treated_as_literal(client, db):
    name = "x'; DROP TABLE tasks;--"
    r = _post(client, name)
    assert r.status_code == 201
    task_id = r.json()["id"]
    got = client.get(f"/v1/tasks/{task_id}", headers=_hdr("read"))
    assert got.status_code == 200
    assert got.json()["name"] == name
    lst = client.get("/v1/tasks", params={"status": "pending' OR '1'='1"}, headers=_hdr("read"))
    assert lst.status_code in (200, 422)
    if lst.status_code == 200:
        assert lst.json()["items"] == []
    cur = client.get("/v1/tasks", params={"cursor": "' UNION SELECT 1--"}, headers=_hdr("read"))
    assert cur.status_code in (200, 422)
    assert cur.status_code != 500
    assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_fr01_post_task_unauthenticated_returns_401(client):
    r = client.post("/v1/tasks", json={"name": "a", "command": "echo hi"})
    _assert_problem(r, 401, "/errors/unauthenticated")


def test_fr01_get_task_unauthenticated_returns_401(client):
    _assert_problem(client.get(f"/v1/tasks/{UNKNOWN_ID}"), 401, "/errors/unauthenticated")


def test_fr01_list_tasks_unauthenticated_returns_401(client):
    _assert_problem(client.get("/v1/tasks"), 401, "/errors/unauthenticated")


def test_fr01_delete_task_unauthenticated_returns_401(client):
    _assert_problem(client.delete(f"/v1/tasks/{UNKNOWN_ID}"), 401, "/errors/unauthenticated")


def test_fr01_post_task_read_key_returns_403(client):
    _assert_problem(_post(client, "denied", command="echo hi", scope="read"), 403, "/errors/forbidden")


def test_fr01_delete_task_write_key_returns_403(client):
    task_id = _post(client, "keepme").json()["id"]
    r = client.delete(f"/v1/tasks/{task_id}", headers=_hdr("write"))
    _assert_problem(r, 403, "/errors/forbidden")
