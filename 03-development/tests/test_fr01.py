"""FR-01: task resource CRUD API (POST/GET/GET list/DELETE /v1/tasks)."""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine
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


def _problem(resp):
    """Return (content_type, problem_type) of an error response."""
    return resp.headers["content-type"], resp.json()["type"]


class _SqlProbe:
    """Counts transactions begun and OFFSET-bearing statements on any engine."""

    def __init__(self):
        self.transactions = 0
        self.offset_statements = 0

    def _begin(self, conn):
        self.transactions += 1

    def _exec(self, conn, cursor, statement, params, context, executemany):
        if "OFFSET" in statement.upper():
            self.offset_statements += 1

    def __enter__(self):
        event.listen(Engine, "begin", self._begin)
        event.listen(Engine, "before_cursor_execute", self._exec)
        return self

    def __exit__(self, *exc):
        event.remove(Engine, "begin", self._begin)
        event.remove(Engine, "before_cursor_execute", self._exec)


def _tasks_in(db, task_id):
    return db.scalar(select(func.count()).select_from(Task).where(Task.id == task_id))


def test_fr01_post_task_write_key_returns_201_with_id(client, db):
    expected_status = "201"
    r = _post(client, "build-docs", command="echo hello")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_task_id = r.json()["id"]
    assert len(result_task_id) > 0
    result_persisted_row_count = db.scalar(select(func.count()).select_from(Task).where(Task.id == result_task_id))
    assert result_persisted_row_count == 1


def test_fr01_get_task_read_key_returns_all_fields(client):
    expected_status = "200"
    expected_fields = "id,command,name,status,created_at"
    task_id = _post(client, "build-docs").json()["id"]
    r = client.get(f"/v1/tasks/{task_id}", headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    body = r.json()
    result_body_keys = list(body.keys())
    assert all(f in result_body_keys for f in expected_fields.split(","))
    assert body["id"] == task_id
    assert body["name"] == "build-docs"
    assert body["command"] == "echo hello"


def test_fr01_list_tasks_supports_status_limit_cursor(client):
    expected_status = "200"
    expected_count = "2"
    _seed(client, 5)
    seen, cursor = [], None
    for page in range(3):
        params = {"status": "pending", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/v1/tasks", params=params, headers=_hdr("read"))
        result_status_code = r.status_code
        assert result_status_code == int(expected_status)
        body = r.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body.get("next_cursor")
        if page == 0:
            result_items = body["items"]
            assert len(result_items) == int(expected_count)
            result_next_cursor_present = bool(cursor)
            assert result_next_cursor_present == True  # noqa: E712
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_fr01_delete_task_admin_removes_task_and_results_in_one_transaction(client, db):
    task_id = _post(client, "doomed").json()["id"]
    for i in range(2):
        db.add(TaskResult(id=str(uuid.uuid4()), task_id=task_id, exit_code=i, stdout_tail="", stderr_tail="", duration_ms=1))
    db.commit()
    with _SqlProbe() as probe:
        r = client.delete(f"/v1/tasks/{task_id}", headers=_hdr("admin"))
    result_status_code = r.status_code
    assert result_status_code >= 200 and result_status_code < 300
    db.expire_all()
    result_task_rows_after = _tasks_in(db, task_id)
    assert result_task_rows_after == 0
    result_result_rows_after = db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task_id))
    assert result_result_rows_after == 0
    result_transaction_count = probe.transactions
    assert result_transaction_count == 1
    assert client.get(f"/v1/tasks/{task_id}", headers=_hdr("read")).status_code == 404


def test_fr01_invalid_body_returns_422_problem_json(client):
    expected_status = "422"
    expected_type = "/errors/validation"
    r = _post(client, "empty-cmd", command="")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_empty_name_rejected_422(client):
    expected_status = "422"
    expected_type = "/errors/validation"
    r = _post(client, "", command="echo hi")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_command_at_1000_chars_accepted(client):
    expected_status = "201"
    command_length = "1000"
    assert float(command_length) <= 1000
    r = _post(client, "max-len", command="a" * int(command_length))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)


def test_fr01_command_above_1000_chars_rejected_422(client):
    expected_status = "422"
    expected_type = "/errors/validation"
    command_length = "1001"
    assert float(command_length) > 1000
    r = _post(client, "too-long", command="a" * int(command_length))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_unknown_id_returns_404_problem_json(client):
    expected_status = "404"
    expected_type = "/errors/not-found"
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}", headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_pagination_is_cursor_based_not_offset(client):
    expected_status = "200"
    ids = _seed(client, 25)
    with _SqlProbe() as probe:
        r = client.get("/v1/tasks", params={"limit": 10}, headers=_hdr("read"))
        r2 = client.get("/v1/tasks", params={"limit": 10, "cursor": r.json()["next_cursor"]}, headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    body = r.json()
    assert len(body["items"]) == 10
    result_next_cursor_present = bool(body["next_cursor"])
    assert result_next_cursor_present == True  # noqa: E712
    result_sql_offset_count = probe.offset_statements
    assert result_sql_offset_count == 0
    # offset is not a supported pagination mechanism: it must not change the page
    r_off = client.get("/v1/tasks", params={"limit": 10, "offset": 10}, headers=_hdr("read"))
    assert [i["id"] for i in r_off.json()["items"]] == [i["id"] for i in body["items"]]
    # the cursor advances to the next, non-overlapping page
    assert r2.status_code == 200
    page1 = {i["id"] for i in body["items"]}
    page2 = {i["id"] for i in r2.json()["items"]}
    assert len(page2) == 10
    assert not page1 & page2
    assert (page1 | page2) <= set(ids)


def test_fr01_list_default_limit_50_max_200_over_limit_422(client):
    expected_status = "200"
    expected_count = "50"
    _seed(client, 60)
    r = client.get("/v1/tasks", headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_items = r.json()["items"]
    assert len(result_items) == int(expected_count)


def test_fr01_list_limit_200_accepted(client):
    expected_status = "200"
    expected_count = "200"
    limit = "200"
    assert float(limit) <= 200
    _seed(client, 250)
    r = client.get("/v1/tasks", params={"limit": limit}, headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_items = r.json()["items"]
    assert len(result_items) == int(expected_count)


def test_fr01_list_limit_201_rejected_422(client):
    expected_status = "422"
    expected_type = "/errors/validation"
    limit = "201"
    assert float(limit) > 200
    r = client.get("/v1/tasks", params={"limit": limit}, headers=_hdr("read"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_duplicate_name_returns_409(client):
    expected_status = "409"
    expected_type = "/errors/conflict"
    responses = [_post(client, "build-docs") for _ in range(2)]
    posts_sent = str(len(responses))
    assert float(posts_sent) > 1
    assert responses[0].status_code == 201
    r = responses[1]
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_sec_t07_sql_injection_payload_treated_as_literal(client, db):
    expected_status = "201"
    name = "x'; DROP TABLE tasks;--"
    r = _post(client, name)
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    task_id = r.json()["id"]
    got = client.get(f"/v1/tasks/{task_id}", headers=_hdr("read"))
    assert got.status_code == 200
    result_name_roundtrip = got.json()["name"]
    assert result_name_roundtrip == name
    lst = client.get("/v1/tasks", params={"status": "pending' OR '1'='1"}, headers=_hdr("read"))
    assert lst.status_code in (200, 422)
    if lst.status_code == 200:
        assert lst.json()["items"] == []
    cur = client.get("/v1/tasks", params={"cursor": "' UNION SELECT 1--"}, headers=_hdr("read"))
    assert cur.status_code in (200, 422)
    assert cur.status_code != 500
    result_tasks_table_present = inspect(db.get_bind()).has_table("tasks")
    assert result_tasks_table_present == True  # noqa: E712
    assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_fr01_post_task_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    r = client.post("/v1/tasks", json={"name": "a", "command": "echo hi"})
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_get_task_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    r = client.get(f"/v1/tasks/{UNKNOWN_ID}")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_list_tasks_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    r = client.get("/v1/tasks")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_delete_task_unauthenticated_returns_401(client):
    expected_status = "401"
    expected_type = "/errors/unauthenticated"
    r = client.delete(f"/v1/tasks/{UNKNOWN_ID}")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_post_task_read_key_returns_403(client):
    expected_status = "403"
    expected_type = "/errors/forbidden"
    r = _post(client, "denied", command="echo hi", scope="read")
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type


def test_fr01_delete_task_write_key_returns_403(client):
    expected_status = "403"
    expected_type = "/errors/forbidden"
    task_id = _post(client, "keepme").json()["id"]
    r = client.delete(f"/v1/tasks/{task_id}", headers=_hdr("write"))
    result_status_code = r.status_code
    assert result_status_code == int(expected_status)
    result_content_type, result_problem_type = _problem(r)
    assert result_content_type.startswith("application/problem+json")
    assert result_problem_type == expected_type
