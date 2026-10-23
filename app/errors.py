"""One shape for every error the API returns:

    {"status": "error", "error": {"code": "...", "message": "..."}, "request_id": "..."}

`code` is for programs, `message` for people. Nothing from inside the server
(exception text, upstream URLs) is ever put in a response.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .clients.ola import OlaUnavailable
from .clients.overpass import PlacesUnavailable
from .context import request_id_var

logger = logging.getLogger("app.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[Mapping[str, str]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


class BodyTooLarge(HTTPException):
    """A request body bigger than MAX_BODY_BYTES (raised while it is being read).
    An HTTPException, because FastAPI turns any other error raised while reading
    a body into a 400."""

    def __init__(self) -> None:
        super().__init__(status_code=413)


def error_response(status: int, code: str, message: str, headers: Optional[Mapping[str, str]] = None) -> JSONResponse:
    body = {"status": "error", "error": {"code": code, "message": message}}
    request_id = request_id_var.get()
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


_HTTP_CODES = {
    400: ("bad_request", "The request could not be read."),
    401: ("unauthenticated", "Sign in again."),
    403: ("forbidden", "Not allowed."),
    404: ("not_found", "Nothing here."),
    405: ("method_not_allowed", "This address does not take that method."),
    413: ("body_too_large", "The request is too large."),
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(OlaUnavailable)
    async def _maps_down(_: Request, exc: OlaUnavailable) -> JSONResponse:
        if exc.reason == "timeout":
            return error_response(504, "maps_timeout", "The maps service took too long. Try again.")
        if exc.reason in ("circuit_open", "rejected_key"):
            return error_response(503, "maps_unavailable", "The maps service is unavailable. Try again shortly.", {"Retry-After": "15"})
        return error_response(502, "maps_error", "The maps service failed. Try again.")

    @app.exception_handler(PlacesUnavailable)
    async def _places_down(_: Request, exc: PlacesUnavailable) -> JSONResponse:
        if exc.reason == "timeout":
            return error_response(504, "places_timeout", "The places service took too long. Try again.")
        if exc.reason == "circuit_open":
            return error_response(503, "places_unavailable", "The places service is unavailable. Try again shortly.", {"Retry-After": "15"})
        return error_response(502, "places_error", "The places service failed. Try again.")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Which fields are wrong, never what was sent in them.
        fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()})
        return error_response(422, "invalid_request", "Invalid " + ", ".join(fields) + ".")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_CODES.get(exc.status_code, ("http_error", "Request failed."))
        return error_response(exc.status_code, code, message, getattr(exc, "headers", None))

    # Anything else is caught in RequestContextMiddleware (app/middleware.py), so
    # that even an unexpected failure gets the error shape and the request id.
