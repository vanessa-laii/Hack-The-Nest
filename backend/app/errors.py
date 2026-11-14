# The single error envelope from CONTRACTS §5, as an exception plus FastAPI handlers.
# Inputs: ApiError raised anywhere in a request; FastAPI validation / HTTP errors.
# Outputs: JSON responses shaped {"error": {"code", "message", "candidates"?}}.
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Maps each error code to its HTTP status (CONTRACTS §5 table).
# Why one table: route code raises only a *code*, so code and status can never
# disagree. NOT_FOUND (an unknown story id) isn't in the §5 table; it is the
# plain 404 that CONTRACTS §1 lists for the story endpoints.
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
    """The one exception routes raise for an expected failure.

    What: carries a contract error code, a user-facing message and, only for
    PLACE_AMBIGUOUS, the place candidates for the picker (FR-2).
    Why: routes can `raise ApiError(...)` anywhere, and the handler below turns
    it into the exact JSON shape the web app expects.
    """

    def __init__(self, code: str, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.code, self.message, self.candidates = code, message, candidates


def envelope(code: str, message: str, candidates: list[dict] | None = None) -> dict:
    """Builds {"error": {...}}. Candidates are capped at 5 because the contract
    limits the picker to 5 options."""
    body: dict = {"code": code, "message": message}
    if candidates is not None:
        body["candidates"] = candidates[:5]
    return {"error": body}


def install(app: FastAPI) -> None:
    """Registers handlers so EVERY non-2xx response uses the envelope.

    Why catch FastAPI's own errors too: by default FastAPI returns 422 with its
    own {"detail": [...]} shape for a bad body, and {"detail": "..."} for a 404.
    The web app would then need to handle three shapes. The contract says one.
    """

    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        return JSONResponse(envelope(exc.code, exc.message, exc.candidates), status_code=STATUS.get(exc.code, 500))

    # A request body that fails Pydantic validation. The contract wants 400
    # INVALID_REQUEST, not FastAPI's default 422. (422 means NO_LAND_DATA here.)
    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError):
        return JSONResponse(envelope("INVALID_REQUEST", "Something went wrong with that request."), status_code=400)

    # Starlette's own HTTP errors (unknown route, missing /files/ path, wrong method).
    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST" if exc.status_code < 500 else "INTERNAL"
        return JSONResponse(envelope(code, str(exc.detail)), status_code=exc.status_code)

    # Last resort for bugs. The message is deliberately generic. Why: an
    # exception's text can include file paths or request URLs, so it must
    # never reach the browser.
    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception):
        return JSONResponse(envelope("INTERNAL", "Internal error."), status_code=500)
