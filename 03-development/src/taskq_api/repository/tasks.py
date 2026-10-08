"""Task persistence.

[FR-01] Citations: SPEC.md:79-91.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from taskq_api.models.task import Task
from taskq_api.models.task_result import TaskResult


def add(session: Session, task: Task) -> Task:
    """Insert a task (flush only)."""
    session.add(task)
    session.flush()
    return task


def get(session: Session, task_id: str) -> Optional[Task]:
    """Fetch a task by id."""
    return session.get(Task, task_id)


def list_page(session: Session, status: Optional[str], limit: int,
              after: Optional[tuple[datetime, str]]) -> list[Task]:
    """Keyset page ordered by (created_at, id); fetches up to `limit` rows."""
    stmt = select(Task).order_by(Task.created_at, Task.id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if after is not None:
        stmt = stmt.where(tuple_(Task.created_at, Task.id) > tuple_(after[0], after[1]))
    # suffix, not .limit(): the sqlite dialect renders .limit() as 'LIMIT ? OFFSET ?'
    return list(session.scalars(stmt.suffix_with("LIMIT", str(int(limit)))))


def delete_with_results(session: Session, task: Task) -> None:
    """Delete result rows and the task (no commit)."""
    session.execute(delete(TaskResult).where(TaskResult.task_id == task.id))
    session.execute(delete(Task).where(Task.id == task.id))


def count_by_status(session: Session) -> dict[str, int]:
    """Return task counts grouped by status."""
    rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {status: count for status, count in rows}


def durations_ms(session: Session) -> list[int]:
    """Recorded execution durations in milliseconds. [FR-09]"""
    values = session.scalars(select(TaskResult.duration_ms).where(TaskResult.duration_ms.is_not(None)))
    return [v for v in values if v is not None]
