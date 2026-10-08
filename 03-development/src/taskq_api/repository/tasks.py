"""Task persistence.

[FR-01] Citations: SPEC.md:79-91.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, literal_column, select, tuple_
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
    stmt = select(Task).order_by(Task.created_at, Task.id).limit(literal_column(str(int(limit))))  # literal: sqlite would otherwise emit OFFSET ?
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if after is not None:
        stmt = stmt.where(tuple_(Task.created_at, Task.id) > tuple_(after[0], after[1]))
    return list(session.scalars(stmt))


def delete_with_results(session: Session, task: Task) -> None:
    """Delete result rows and the task (no commit)."""
    session.execute(delete(TaskResult).where(TaskResult.task_id == task.id))
    session.execute(delete(Task).where(Task.id == task.id))
