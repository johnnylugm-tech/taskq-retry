"""API key ORM model (SHA-256 hash only, never plaintext).

[FR-01] Citations: SPEC.md:104, SPEC.md:309.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from taskq_api.models.base import Base
from taskq_api.models.task import _now


class ApiKey(Base):
    """Stored API key: sha256 hex digest plus scope."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
