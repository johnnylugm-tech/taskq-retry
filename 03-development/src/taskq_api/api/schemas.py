"""Pydantic request/response models.

[FR-01] Citations: SPEC.md:82, SPEC.md:88, SPEC.md:90-91.
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from taskq_api.service.validation import MAX_COMMAND_LEN, validate_command

TaskStatus = Literal["pending", "running", "succeeded", "failed"]


class TaskCreate(BaseModel):
    """POST /v1/tasks body."""

    name: str = Field(min_length=1, max_length=255)
    command: str = Field(min_length=1, max_length=MAX_COMMAND_LEN)

    _check_command = field_validator("command")(validate_command)


class TaskOut(BaseModel):
    """Full task representation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    command: str
    name: str
    status: str
    created_at: datetime


class TaskPage(BaseModel):
    """Cursor-paginated task list."""

    items: list[TaskOut]
    next_cursor: Optional[str] = None
