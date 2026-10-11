"""Catch-all handler for unexpected exceptions.

[FR-10] Citations: SPEC.md:166, SPEC.md:344-347. CancelledError derives from
BaseException, so it is never caught here and propagates (AC-10.6).
"""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from taskq_api.errors import internal_error

logger = logging.getLogger("taskq_api.errors")


def install_unexpected_handler(app: FastAPI) -> None:
    """Map any unexpected Exception to a 500 problem+json. [FR-10]"""

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled %s", type(exc).__name__, exc_info=exc)
        return internal_error(request)
