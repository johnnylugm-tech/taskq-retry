"""Database engine and per-request session.

[FR-01] Citations: SPEC.md:52, SPEC.md:304-312.
"""
from typing import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

__all__ = ["Engine", "IntegrityError", "Session", "create_engine", "get_session"]


def get_session(request: Request) -> Iterator[Session]:
    """Yield one session per request; services commit explicitly."""
    with Session(request.app.state.engine) as session:
        yield session
