"""X-API-Key authentication and scope authorization.

[FR-03/FR-04] Citations: SPEC.md:101-113.
"""
import hashlib
import hmac
from typing import Callable

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from taskq_api.db import get_session
from taskq_api.errors import ApiError
from taskq_api.repository import api_keys as repo

# Hierarchical scopes: a higher rank includes every lower one (AC-4.1).
_RANK = {"read": 1, "write": 2, "admin": 3}


def hash_key(plaintext: str) -> str:
    """SHA-256 hex digest under which a key is stored."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _unauthenticated() -> ApiError:
    return ApiError(401, "/errors/unauthenticated", "Unauthenticated", "missing or invalid API key")


def require_scope(scope: str) -> Callable:
    """Build a dependency requiring at least `scope`; returns the open session."""

    def dep(x_api_key: str | None = Header(default=None), session: Session = Depends(get_session)) -> Session:
        if not x_api_key:
            raise _unauthenticated()
        digest = hash_key(x_api_key)
        row = repo.get_active_by_hash(session, digest)
        if row is None or not hmac.compare_digest(row.key_hash, digest):
            raise _unauthenticated()
        if _RANK.get(row.scope, 0) < _RANK[scope]:
            raise ApiError(403, "/errors/forbidden", "Forbidden", "insufficient scope")
        return session

    return dep
