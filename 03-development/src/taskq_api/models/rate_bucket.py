"""Rate-limit token bucket ORM model (one row per API key).

[FR-05] Citations: SPEC.md:115-120.
"""
from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from taskq_api.models.base import Base


class RateBucket(Base):
    """Persisted bucket state: remaining tokens and the epoch second of the last refill."""

    __tablename__ = "rate_buckets"

    key_id: Mapped[str] = mapped_column(String(36), ForeignKey("api_keys.id"), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    refilled_at: Mapped[float] = mapped_column(Float, nullable=False)
