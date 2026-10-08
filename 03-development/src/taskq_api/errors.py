"""RFC 7807 problem+json errors.

[FR-01] Citations: SPEC.md:88-89, SPEC.md:164-168.
"""
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"


class ApiError(Exception):
    """Error carrying an HTTP status and problem type."""

    def __init__(self, status: int, type_: str, title: str, detail: str = "",
                 headers: dict[str, str] | None = None):
        super().__init__(title)
        self.headers = headers
        self.status = status
        self.type = type_
        self.title = title
        self.detail = detail


def _problem(request: Request, status: int, type_: str, title: str, detail: str,
             headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        headers=headers,
        media_type=PROBLEM_JSON,
        content={
            "type": type_,
            "title": title,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
            "correlation_id": str(uuid.uuid4()),
        },
    )


def install_handlers(app: FastAPI) -> None:
    """Register problem+json handlers on the app."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _problem(request, exc.status, exc.type, exc.title, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return _problem(request, 422, "/errors/validation", "Validation error", detail)
