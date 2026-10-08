"""X-API-Key authentication and scope authorization.

[FR-01] Citations: SPEC.md:68-69, SPEC.md:103-112.
"""
import hashlib
import hmac
from typing import Callable

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from taskq_api.db import get_session
from taskq_api.errors import ApiError
from taskq_api.models.api_key import ApiKey

_RANK = {"read": 1, "write": 2, "admin": 3}


def require_scope(scope: str) -> Callable:
    """Build a dependency requiring at least `scope`; returns the open session."""

    def dep(x_api_key: str | None = Header(default=None), session: Session = Depends(get_session)) -> Session:
        if not x_api_key:
            raise ApiError(401, "/errors/unauthenticated", "Unauthenticated", "missing or invalid API key")
        digest = hashlib.sha256(x_api_key.encode()).hexdigest()
        row = session.scalar(select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None)))
        if row is None or not hmac.compare_digest(row.key_hash, digest):
            raise ApiError(401, "/errors/unauthenticated", "Unauthenticated", "missing or invalid API key")
        if _RANK.get(row.scope, 0) < _RANK[scope]:
            raise ApiError(403, "/errors/forbidden", "Forbidden", "insufficient scope")
        return session

    return dep
