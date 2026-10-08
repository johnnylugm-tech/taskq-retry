"""Task business logic.

[FR-01] Citations: SPEC.md:79-91, SPEC.md:360-364.
"""
import base64
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from taskq_api.errors import ApiError
from taskq_api.models.task import Task
from taskq_api.repository import tasks as repo


def _not_found() -> ApiError:
    return ApiError(404, "/errors/not-found", "Not found", "task not found")


def create(session: Session, name: str, command: str) -> Task:
    """Create a task; duplicate name -> 409."""
    try:
        task = repo.add(session, Task(name=name, command=command))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(409, "/errors/conflict", "Conflict", "task name already exists")
    return task


def get(session: Session, task_id: str) -> Task:
    """Fetch a task or raise 404."""
    task = repo.get(session, task_id)
    if task is None:
        raise _not_found()
    return task


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, task_id = raw.split("|", 1)
        return datetime.fromisoformat(ts), task_id
    except ValueError:
        raise ApiError(422, "/errors/validation", "Validation error", "invalid cursor")


def _encode_cursor(task: Task) -> str:
    return base64.urlsafe_b64encode(f"{task.created_at.isoformat()}|{task.id}".encode()).decode()


def list_page(session: Session, status: Optional[str], limit: int, cursor: Optional[str]):
    """Return (items, next_cursor) using keyset pagination."""
    after = _decode_cursor(cursor) if cursor else None
    rows = repo.list_page(session, status, limit + 1, after)
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1]) if len(rows) > limit else None
    return items, next_cursor


def delete(session: Session, task_id: str) -> None:
    """Delete a task and its results in one transaction."""
    task = get(session, task_id)
    repo.delete_with_results(session, task)
    session.commit()
