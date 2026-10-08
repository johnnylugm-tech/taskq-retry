"""Per-key DB-backed token bucket.

[FR-05] Citations: SPEC.md:115-120.
"""
import math
import time
from dataclasses import dataclass

from taskq_api.db import IntegrityError, Session
from taskq_api.models.rate_bucket import RateBucket
from taskq_api.repository import rate_buckets as repo


@dataclass(frozen=True)
class Decision:
    """Outcome of one token request: allowed, else seconds until a token is available. [FR-05]"""

    allowed: bool
    retry_after: int = 0


def refill(tokens: float, rate_burst: int, rate_per_sec: float, elapsed_seconds: float) -> float:
    """Tokens after `elapsed_seconds` of refill, capped at `rate_burst`. [FR-05]"""
    return min(float(rate_burst), tokens + rate_per_sec * elapsed_seconds)


def _locked_bucket(session: Session, key_id: str, rate_burst: int, now: float) -> RateBucket:
    bucket = repo.get_for_update(session, key_id)
    if bucket is not None:
        return bucket
    try:
        with session.begin_nested():
            return repo.add(session, RateBucket(key_id=key_id, tokens=float(rate_burst), refilled_at=now))
    except IntegrityError:  # a concurrent first request created the row
        bucket = repo.get_for_update(session, key_id)
        if bucket is None:
            raise
        return bucket


def consume(session: Session, key_id: str, rate_burst: int, rate_per_sec: float) -> Decision:
    """Take one token for `key_id` in a single row-locked transaction, committing the new state. [FR-05]"""
    repo.begin_exclusive(session)
    now = time.time()
    bucket = _locked_bucket(session, key_id, rate_burst, now)
    tokens = refill(bucket.tokens, rate_burst, rate_per_sec, max(0.0, now - bucket.refilled_at))
    allowed = tokens >= 1.0
    bucket.tokens = tokens - 1.0 if allowed else tokens
    bucket.refilled_at = now
    session.commit()
    if allowed:
        return Decision(True)
    return Decision(False, max(1, math.ceil((1.0 - tokens) / rate_per_sec)))
