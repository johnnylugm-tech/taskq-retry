"""FastAPI application factory.

[FR-01] Citations: SPEC.md:79-91.
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import create_engine

from taskq_api.api import routes_runs, routes_tasks
from taskq_api.errors import install_handlers


def create_app() -> FastAPI:
    """Create the app, bound to the engine at TASKQ_DB_URL."""
    engine = create_engine(os.environ["TASKQ_DB_URL"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="taskq", lifespan=lifespan)
    app.state.engine = engine
    install_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        return {"status": "ready"}

    app.include_router(routes_tasks.router)
    app.include_router(routes_runs.router)
    return app
