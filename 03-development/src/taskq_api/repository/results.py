"""Task result persistence.

[FR-02] Citations: SPEC.md:98-99.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from taskq_api.models.task_result import INITIAL_STATUS, TaskResult


def add(session: Session, task_id: str, run_id: str) -> TaskResult:
    """Insert a pending run row and commit."""
    row = TaskResult(id=run_id, task_id=task_id, status=INITIAL_STATUS)
    session.add(row)
    session.commit()
    return row


def get(session: Session, run_id: str) -> TaskResult | None:
    """Fetch a run row by id, or None."""
    return session.get(TaskResult, run_id)


def list_for_task(session: Session, task_id: str) -> list[TaskResult]:
    """Runs of a task, newest first."""
    stmt = (select(TaskResult).where(TaskResult.task_id == task_id)
            .order_by(TaskResult.finished_at.desc(), TaskResult.id.desc()))
    return list(session.scalars(stmt))
