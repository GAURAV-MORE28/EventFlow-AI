"""Error envelope (00_SHARED_CONTRACT.md §3).

Every non-2xx response from every endpoint has the same shape. There is exactly one
place that builds it, so there is exactly one place that can get it wrong.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("eventflow.errors")

# 00 §3 — code -> HTTP status.
CODE_STATUS: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "INVALID_SCENARIO": 400,
    "INVALID_HORIZON": 400,
    "ENTITY_NOT_FOUND": 404,
    "INTERVENTION_NOT_FOUND": 404,
    "SIMULATION_NOT_FOUND": 404,
    "NUDGE_NOT_FOUND": 404,
    "INTERVENTION_ALREADY_RESOLVED": 409,
    "INTERVENTION_EXPIRED": 409,
    "MODEL_NOT_READY": 422,
    "INSUFFICIENT_HISTORY": 422,
    "INTERNAL_ERROR": 500,
    "ML_MODULE_UNAVAILABLE": 503,
}


class ApiError(Exception):
    """Raise this anywhere; the handler renders the envelope."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def status_code(self) -> int:
        return CODE_STATUS.get(self.code, 500)


def envelope(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=envelope(
                "INVALID_REQUEST",
                "Request body or query parameters failed validation.",
                {"errors": exc.errors()[:5]},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {400: "INVALID_REQUEST", 404: "ENTITY_NOT_FOUND", 409: "INTERVENTION_ALREADY_RESOLVED"}.get(
            exc.status_code, "INTERNAL_ERROR"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # 01 §8: "never a bare 500" — an unhandled error still leaves as an envelope.
        log.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content=envelope("INTERNAL_ERROR", "An unexpected error occurred."),
        )
