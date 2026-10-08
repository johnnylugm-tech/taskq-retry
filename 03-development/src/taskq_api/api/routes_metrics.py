"""Metrics route.

[FR-04] Citations: SPEC.md:158 (GET /v1/metrics requires scope admin), SPEC.md:113.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from taskq_api.api.deps import require_scope
from taskq_api.models.task import Task

router = APIRouter(prefix="/v1", tags=["metrics"])


@router.get("/metrics", summary="Metrics", description="Task counts by status (scope admin).")
def get_metrics(session: Session = Depends(require_scope("admin"))):
    rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {"task_counts": {status: count for status, count in rows}}
