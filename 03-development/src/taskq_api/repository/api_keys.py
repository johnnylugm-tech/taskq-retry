"""API key persistence.

[FR-03] Citations: SPEC.md:104-106.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from taskq_api.models.api_key import ApiKey


def add(session: Session, key: ApiKey) -> ApiKey:
    """Insert a key row (flush only)."""
    session.add(key)
    session.flush()
    return key


def get_active_by_hash(session: Session, key_hash: str) -> Optional[ApiKey]:
    """Return the non-revoked key with this hash, else None."""
    return session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)))
