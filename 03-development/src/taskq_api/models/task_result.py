"""Task result ORM model.

[FR-01] [FR-02] Citations: SPEC.md:97-98, SPEC.md:312.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from taskq_api.models.base import Base
from taskq_api.models.task import _now

INITIAL_STATUS = "pending"


class TaskResult(Base):
    """Execution result row belonging to a task."""

    __tablename__ = "task_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    stdout_tail: Mapped[str] = mapped_column(Text, default="")
    stderr_tail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
