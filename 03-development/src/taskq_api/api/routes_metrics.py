"""Metrics route.

[FR-04] Citations: SPEC.md:158 (GET /v1/metrics requires scope admin), SPEC.md:113.
"""
from fastapi import APIRouter, Depends

from taskq_api.api.deps import require_scope
from taskq_api.db import Session
from taskq_api.repository import tasks as repo

router = APIRouter(prefix="/v1", tags=["metrics"])


@router.get("/metrics", summary="Metrics", description="Task counts by status (scope admin).")
def get_metrics(session: Session = Depends(require_scope("admin"))) -> dict[str, dict[str, int]]:
    return {"task_counts": repo.count_by_status(session)}
