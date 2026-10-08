"""Engine, pool and request-scoped session; the only place a Session is opened.

[FR-06] Citations: SPEC.md:122-130. Commit on success, rollback on exception, close always.
"""
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from taskq_api import config

# Layer-neutral names so service/api never spell `Session` or import sqlalchemy.
DbSession = Session
DbEngine = Engine
DbIntegrityError = IntegrityError
DbUnavailableError = OperationalError


def create_db_engine(url: str) -> Engine:
    """Build an engine with TASKQ_DB_POOL_SIZE connections and pre-ping. [FR-06]"""
    return create_engine(url, pool_size=config.db_pool_size(), pool_pre_ping=True)


@contextmanager
def session_scope(engine: Engine, commit: bool = True) -> Iterator[Session]:
    """One transaction: commit on success, rollback on exception, connection always released. [FR-06]

    `commit=False` is for request sessions whose services commit their own writes
    (e.g. exactly one commit per rate-limit update, FR-05); rollback and close still apply.
    """
    session = Session(engine)
    try:
        yield session
        if commit:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding one session per request; rollback on exception, always closed. [FR-06]"""
    with session_scope(request.app.state.engine, commit=False) as session:
        yield session
