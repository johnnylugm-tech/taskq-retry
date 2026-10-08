"""FastAPI application factory.

[FR-01] Citations: SPEC.md:79-91.
[FR-04] Citations: SPEC.md:113 (all /v1 routes visible with the shared dependency).
"""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from taskq_api.api import routes_metrics, routes_runs, routes_tasks
from taskq_api.api.middleware import install_rate_limit
from taskq_api.errors import install_handlers, service_unavailable
from taskq_api.repository.session import DbUnavailableError, create_db_engine


def create_app() -> FastAPI:
    """Create the app, bound to the engine at TASKQ_DB_URL."""
    engine = create_db_engine(os.environ["TASKQ_DB_URL"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="taskq", lifespan=lifespan)
    app.state.engine = engine
    install_handlers(app)
    app.add_exception_handler(DbUnavailableError, lambda request, exc: service_unavailable(request))
    install_rate_limit(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        return {"status": "ready"}

    # [FR-04] register APIRoutes directly so every /v1 route is inspectable on app.routes.
    for router in (routes_tasks.router, routes_runs.router, routes_metrics.router):
        app.router.routes.extend(router.routes)
    return app
