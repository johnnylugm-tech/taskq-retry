"""Database engine and per-request session.

[FR-01] Citations: SPEC.md:52, SPEC.md:304-312.
"""
from typing import Iterator

from fastapi import Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    """Yield one session per request; services commit explicitly."""
    with Session(request.app.state.engine) as session:
        yield session
