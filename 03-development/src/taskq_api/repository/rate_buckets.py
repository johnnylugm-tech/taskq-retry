"""Rate bucket persistence.

[FR-05] Citations: SPEC.md:119.
"""
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from taskq_api.models.rate_bucket import RateBucket


def begin_exclusive(session: Session) -> None:
    """Serialize writers for the coming read-modify-write.

    SQLite ignores row locks, so take the database write lock up front (SAD
    section 5); other dialects rely on SELECT ... FOR UPDATE in `get_for_update`.
    [FR-05]
    """
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def get_for_update(session: Session, key_id: str) -> Optional[RateBucket]:
    """Return the key's bucket row locked FOR UPDATE, else None. [FR-05]"""
    return session.scalar(select(RateBucket).where(RateBucket.key_id == key_id).with_for_update())


def add(session: Session, bucket: RateBucket) -> RateBucket:
    """Insert a bucket row (flush only). [FR-05]"""
    session.add(bucket)
    session.flush()
    return bucket
