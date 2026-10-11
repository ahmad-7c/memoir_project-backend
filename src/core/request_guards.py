"""
@file src/core/request_guards.py
@description Two request-shape guards applied to every request, before any
route runs: a body-size cap and a Content-Type check on bodies that are
supposed to be JSON.

Both are raw ASGI (not BaseHTTPMiddleware) for the same reason as
SecurityHeadersMiddleware: no response buffering.
"""

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.responses import JSONResponse

# No route in this API accepts a file body directly -- every upload goes
# straight to Supabase Storage via a presigned URL (see api/media.py), so
# this backend itself only ever receives small JSON payloads. 1 MiB is
# generous headroom for the largest legitimate body (a long memory's text)
# while still refusing anything shaped like an attempted abuse.
MAX_BODY_BYTES = 1 * 1024 * 1024

_BODY_METHODS = {"POST", "PUT", "PATCH"}


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        content_length_header = headers.get("content-length")
        content_length = 0
        if content_length_header is not None:
            try:
                content_length = int(content_length_header)
            except ValueError:
                content_length = 0  # Malformed header -- let FastAPI reject it downstream.

        if content_length > MAX_BODY_BYTES:
            response = JSONResponse({"detail": "Request body too large."}, status_code=413)
            await response(scope, receive, send)
            return

        # Only routes that actually HAVE a body need a JSON Content-Type --
        # several routes here are POSTs with no body at all (e.g. creating a
        # share link), and rejecting those on a missing/absent Content-Type
        # would break a legitimate, bodyless request.
        if content_length > 0:
            content_type = headers.get("content-type", "")
            if content_type.split(";")[0].strip().lower() != "application/json":
                response = JSONResponse(
                    {"detail": "Content-Type must be application/json."},
                    status_code=415,
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
