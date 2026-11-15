# The single error envelope from CONTRACTS §5, as an exception plus FastAPI handlers.
# Inputs: ApiError raised anywhere in a request; FastAPI validation / HTTP errors.
# Outputs: JSON responses shaped {"error": {"code", "message", "candidates"?}}.
#
# How it works:
#   1. STATUS maps each error code to its HTTP status.
#   2. Routes raise ApiError(code, message) for expected failures.
#   3. install() registers handlers that turn ApiError, bad request bodies, HTTP errors
#      and unexpected bugs into the same {"error": {...}} shape.
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Error code -> HTTP status (CONTRACTS §5). Routes raise only a code, so code and
# status can't disagree. NOT_FOUND is the plain 404 for an unknown story id (§1).
STATUS = {
    "INVALID_REQUEST": 400,
    "PLACE_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "PLACE_AMBIGUOUS": 409,
    "NO_LAND_DATA": 422,
    "NOT_READY": 425,
    "INTERNAL": 500,
    "UPSTREAM_ERROR": 502,
}


class ApiError(Exception):
    """The one exception routes raise for an expected failure: a contract code, a
    user-facing message and, for PLACE_AMBIGUOUS only, the place candidates (FR-2)."""

    def __init__(self, code: str, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates


def envelope(code: str, message: str, candidates: list[dict] | None = None) -> dict:
    """Builds {"error": {...}}. At most 5 candidates, the contract's picker limit."""
    body: dict = {"code": code, "message": message}
    if candidates is not None:
        body["candidates"] = candidates[:5]
    return {"error": body}


def _code_for_status(status_code: int) -> str:
    """Picks the contract code for one of Starlette's own HTTP errors."""
    if status_code == 404:
        return "NOT_FOUND"
    if status_code < 500:
        return "INVALID_REQUEST"
    return "INTERNAL"


def install(app: FastAPI) -> None:
    """Registers handlers so EVERY non-2xx response uses the envelope.
    FastAPI's own errors use {"detail": ...}; the contract promises the web app one shape."""

    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        body = envelope(exc.code, exc.message, exc.candidates)
        return JSONResponse(body, status_code=STATUS.get(exc.code, 500))

    # A body that fails Pydantic validation. The contract wants 400 INVALID_REQUEST,
    # not FastAPI's default 422 (422 means NO_LAND_DATA here).
    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError):
        body = envelope("INVALID_REQUEST", "Something went wrong with that request.")
        return JSONResponse(body, status_code=400)

    # Starlette's own HTTP errors (unknown route, missing /files/ path, wrong method).
    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _code_for_status(exc.status_code)
        return JSONResponse(envelope(code, str(exc.detail)), status_code=exc.status_code)

    # Last resort for bugs. The message stays generic because an exception's text
    # can contain file paths or URLs that must never reach the browser.
    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception):
        return JSONResponse(envelope("INTERNAL", "Internal error."), status_code=500)
