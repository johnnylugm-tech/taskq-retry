"""Task CRUD routes.

[FR-01] Citations: SPEC.md:79-91.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Response

from taskq_api.db import Session
from taskq_api.api.schemas import TaskCreate, TaskOut, TaskPage, TaskStatus
from taskq_api.api.deps import require_scope
from taskq_api.service import tasks as svc

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.post("", status_code=201, response_model=TaskOut, summary="Create task",
             description="Create a task (scope write).")
def create_task(body: TaskCreate, session: Session = Depends(require_scope("write"))) -> Any:
    return svc.create(session, body.name, body.command)


@router.get("/{task_id}", response_model=TaskOut, summary="Get task",
            description="Fetch one task (scope read).")
def get_task(task_id: str, session: Session = Depends(require_scope("read"))) -> Any:
    return svc.get(session, task_id)


@router.get("", response_model=TaskPage, summary="List tasks",
            description="Cursor-paginated task list (scope read).")
def list_tasks(status: Optional[TaskStatus] = None, limit: int = Query(50, ge=1, le=200),
               cursor: Optional[str] = None, session: Session = Depends(require_scope("read"))) -> TaskPage:
    items, next_cursor = svc.list_page(session, status, limit, cursor)
    return TaskPage(items=items, next_cursor=next_cursor)


@router.delete("/{task_id}", status_code=204, summary="Delete task",
               description="Delete a task and its results (scope admin).")
def delete_task(task_id: str, session: Session = Depends(require_scope("admin"))) -> Response:
    svc.delete(session, task_id)
    return Response(status_code=204)
