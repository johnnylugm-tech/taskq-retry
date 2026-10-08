"""Task run routes.

[FR-02] Citations: SPEC.md:93-99.
"""
import asyncio
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from taskq_api.auth import require_scope
from taskq_api.repository import results as repo
from taskq_api.service import executor
from taskq_api.service import tasks as svc

router = APIRouter(prefix="/v1/tasks", tags=["runs"])
_background: set = set()


class RunAccepted(BaseModel):
    """202 body."""

    run_id: str


class RunOut(BaseModel):
    """One run record."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    run_id: str = Field(validation_alias="id")
    task_id: str
    status: str
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: Optional[int] = None
    finished_at: Optional[datetime] = None


class RunPage(BaseModel):
    """Run history, newest first."""

    items: list[RunOut]


@router.post("/{task_id}/run", status_code=202, response_model=RunAccepted, summary="Run task",
             description="Start asynchronous execution (scope write).")
async def run_task(task_id: str, request: Request, session: Session = Depends(require_scope("write"))):
    task = svc.get(session, task_id)
    run_id = str(uuid.uuid4())
    repo.add(session, task.id, run_id)
    job = asyncio.ensure_future(executor.execute(request.app.state.engine, run_id, task.command))
    _background.add(job)
    job.add_done_callback(_background.discard)
    return RunAccepted(run_id=run_id)


@router.get("/{task_id}/runs", response_model=RunPage, summary="List runs",
            description="Run history, newest first (scope read).")
def list_runs(task_id: str, session: Session = Depends(require_scope("read"))):
    svc.get(session, task_id)
    return RunPage(items=repo.list_for_task(session, task_id))
