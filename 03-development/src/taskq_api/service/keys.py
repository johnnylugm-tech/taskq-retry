"""API key creation.

[FR-03] Citations: SPEC.md:104-105.
"""
import secrets
import uuid

from sqlalchemy.orm import Session

from taskq_api.models.api_key import ApiKey
from taskq_api.repository import api_keys as repo
from taskq_api.service.auth import hash_key


def create_key(session: Session, scope: str) -> str:
    """Store the SHA-256 of a fresh key and return the plaintext (shown once)."""
    plaintext = "tq-" + secrets.token_urlsafe(32)
    repo.add(session, ApiKey(
        id=str(uuid.uuid4()),
        key_hash=hash_key(plaintext),
        scope=scope,
    ))
    session.commit()
    return plaintext
