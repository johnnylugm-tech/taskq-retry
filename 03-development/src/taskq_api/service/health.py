"""Readiness check: DB reachable and migration at head (fail closed).

[FR-09] Citations: SPEC.md:157, SPEC.md:160, SPEC.md:366-367.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from taskq_api.errors import ApiError
from taskq_api.repository import health as repo
from taskq_api.repository.session import DbEngine

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def head_revision() -> str:
    """Head revision of the bundled alembic scripts. [FR-09]"""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    return str(ScriptDirectory.from_config(config).get_current_head())


def _not_ready(detail: str) -> ApiError:
    return ApiError(503, "/errors/not-ready", "Service Unavailable", detail)


def check_ready(engine: DbEngine) -> dict[str, str]:
    """Return ready status or raise a 503 naming the failed check. [FR-09]"""
    try:
        repo.ping(engine)
        current = repo.current_revision(engine)
    except Exception:  # detail is fixed text so connection URLs/secrets never leak
        raise _not_ready("database unavailable") from None
    if current != head_revision():
        raise _not_ready("migration not at head")
    return {"status": "ready"}
