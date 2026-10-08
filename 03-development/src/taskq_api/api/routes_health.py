"""Liveness and readiness routes (no auth).

[FR-09] Citations: SPEC.md:156-157.
"""
from fastapi import APIRouter, Request

from taskq_api.service import health as svc

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness", description="Process is alive.")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness", description="DB reachable and migration at head, else 503.")
def readyz(request: Request) -> dict[str, str]:
    return svc.check_ready(request.app.state.engine)
