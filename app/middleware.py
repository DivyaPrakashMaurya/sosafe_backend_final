"""Around every request: a request id, a size limit, and one access-log line.

Plain ASGI (not BaseHTTPMiddleware) so it adds next to nothing per request.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context import request_id_var
from .errors import BodyTooLarge, error_response

logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = {"/healthz"}  # asked every few seconds by the load balancer


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        started_response = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, started_response
            if message["type"] == "http.response.start":
                started_response = True
                status = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        try:
            declared = headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
                response = error_response(413, "body_too_large", "The request is too large.")
                await response(scope, receive, send_with_id)
                return

            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_body_bytes:
                        raise BodyTooLarge()
                return message

            try:
                await self.app(scope, limited_receive, send_with_id)
            except Exception:
                logger.exception("unhandled error", extra={"event": "unhandled_error", "path": scope.get("path")})
                if started_response:
                    raise  # too late for a clean error response
                response = error_response(500, "internal_error", "Something went wrong. Try again.")
                await response(scope, receive, send_with_id)
        finally:
            if scope.get("path") not in _QUIET_PATHS:
                # The path only: query strings hold what people searched for.
                logger.info(
                    "request",
                    extra={
                        "event": "request",
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            request_id_var.reset(token)
