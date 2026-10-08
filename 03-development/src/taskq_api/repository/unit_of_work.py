"""Unit of work: opens a transaction for callers that must not hold a Session.

[FR-06] Citations: SPEC.md:122-130.
"""
from types import TracebackType
from typing import Optional

from taskq_api.models.task_result import TaskResult
from taskq_api.repository import results
from taskq_api.repository.session import DbEngine, DbSession, session_scope


class UnitOfWork:
    """Context manager: commit on clean exit, rollback on exception. [FR-06]"""

    def __init__(self, engine: DbEngine) -> None:
        self._scope = session_scope(engine)
        self._session: Optional[DbSession] = None

    def __enter__(self) -> "UnitOfWork":
        self._session = self._scope.__enter__()
        return self

    def __exit__(self, exc_type: Optional[type[BaseException]], exc: Optional[BaseException],
                 tb: Optional[TracebackType]) -> Optional[bool]:
        return self._scope.__exit__(exc_type, exc, tb)

    def get_run(self, run_id: str) -> Optional[TaskResult]:
        """Fetch a run row inside this transaction."""
        return results.get(self._require_session(), run_id)

    def _require_session(self) -> DbSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork used outside its 'with' block")
        return self._session
