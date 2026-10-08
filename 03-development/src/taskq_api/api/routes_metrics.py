"""Metrics route.

[FR-04] Citations: SPEC.md:158 (GET /v1/metrics requires scope admin), SPEC.md:113.
[FR-09] Citations: SPEC.md:158 (counts, latency percentiles, rate-limit rejections).
"""
from typing import Any

from fastapi import APIRouter, Depends, Request

from taskq_api.api.deps import require_scope
from taskq_api.repository.session import DbSession
from taskq_api.service import metrics as svc

router = APIRouter(prefix="/v1", tags=["metrics"])


@router.get("/metrics", summary="Metrics",
            description="Task counts, latency percentiles, rate-limit rejections (scope admin).")
def get_metrics(request: Request, session: DbSession = Depends(require_scope("admin"))) -> dict[str, Any]:
    return svc.snapshot(session, request.app.state.rejections)
