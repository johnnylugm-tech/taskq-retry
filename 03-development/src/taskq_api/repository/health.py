"""Readiness probes against the database.

[FR-09] Citations: SPEC.md:157, SPEC.md:160.
"""
from sqlalchemy import text

from taskq_api.repository.session import DbEngine


def ping(engine: DbEngine) -> None:
    """Run `SELECT 1`; raises when the database cannot be reached. [FR-09]"""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def current_revision(engine: DbEngine) -> str | None:
    """Revision in `alembic_version`, or None when the table is absent or empty. [FR-09]"""
    with engine.connect() as conn:
        if not engine.dialect.has_table(conn, "alembic_version"):
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def schema_present(engine: DbEngine) -> bool:
    """True when every ORM table exists (schema bootstrapped without alembic). [FR-09]"""
    from taskq_api.models import api_key, rate_bucket, task, task_result  # noqa: F401  (register tables)
    from taskq_api.models.base import Base

    with engine.connect() as conn:
        return all(engine.dialect.has_table(conn, name) for name in Base.metadata.tables)
