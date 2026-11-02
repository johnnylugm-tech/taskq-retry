"""FastAPI application factory.

[FR-01] Citations: SPEC.md:79-91.
[FR-08] Citations: SPEC.md:145-150 (executor owned by the lifespan, drained on shutdown).
[FR-09] Citations: SPEC.md:156-158 (health and metrics routers).
[FR-04] Citations: SPEC.md:113 (all /v1 routes visible with the shared dependency).
"""
import json
import logging
import logging.config
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from taskq_api import config
from taskq_api.api import routes_health, routes_metrics, routes_runs, routes_tasks
from taskq_api.api.error_handlers import install_unexpected_handler
from taskq_api.api.middleware import install_cors, install_rate_limit
from taskq_api.errors import install_handlers, service_unavailable
from taskq_api.service.executor import Executor
from taskq_api.service.metrics import RejectionCounter
from taskq_api.service.redaction import redact
from taskq_api.repository.session import DbUnavailableError, create_db_engine


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, and exc when there is a traceback. [NFR-04]"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """Plain text line; secrets are redacted after formatting. [NFR-04]"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str, fmt: str) -> None:
    """Send taskq_api and uvicorn logs to stderr at TASKQ_LOG_LEVEL in TASKQ_LOG_FORMAT. [NFR-04]

    Citations: SPEC.md:299-300. Safe to call repeatedly: dictConfig replaces the one handler.
    `taskq_api` keeps propagating so a test's caplog still sees its records.
    """
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "taskq_json": {"()": _JsonFormatter},
            "taskq_text": {"()": _TextFormatter, "format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "taskq": {"class": "logging.StreamHandler", "formatter": f"taskq_{fmt}", "stream": "ext://sys.stderr"},
        },
        "loggers": {
            "taskq_api": {"handlers": ["taskq"], "level": level, "propagate": True},
            "uvicorn": {"handlers": ["taskq"], "level": level, "propagate": False},
        },
    })


def create_app() -> FastAPI:
    """Create the app, bound to the engine at TASKQ_DB_URL."""
    configure_logging(config.log_level(), config.log_format())
    engine = create_db_engine(os.environ["TASKQ_DB_URL"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.executor.start()
        yield
        await app.state.executor.drain()
        engine.dispose()

    app = FastAPI(title="taskq", lifespan=lifespan)
    app.state.engine = engine
    app.state.executor = Executor(engine, config.max_concurrent(), config.drain_timeout(), config.task_timeout())
    install_handlers(app)
    install_unexpected_handler(app)
    app.add_exception_handler(DbUnavailableError, lambda request, exc: service_unavailable(request))
    app.state.rejections = RejectionCounter()
    install_rate_limit(app)
    install_cors(app)

    # [FR-04] register APIRoutes directly so every /v1 route is inspectable on app.routes.
    for router in (routes_health.router, routes_tasks.router, routes_runs.router, routes_metrics.router):
        app.router.routes.extend(router.routes)
    return app
