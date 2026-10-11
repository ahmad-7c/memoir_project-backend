"""
@file src/core/request_id.py
@description Stamps every request/response pair with a request id, logged
alongside each request so a specific failure can be found in logs without
correlating by timestamp alone. Accepts an inbound `X-Request-ID` (useful
behind a load balancer or when the frontend forwards one) and always
echoes one back, generating a fresh id if none was supplied.
"""

import logging
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("request")


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or uuid.uuid4().hex
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        method = scope.get("method", "")
        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers_list}
                logger.info("%s %s request_id=%s status=%s", method, path, request_id, message.get("status"))
            await send(message)

        await self.app(scope, receive, send_wrapper)
