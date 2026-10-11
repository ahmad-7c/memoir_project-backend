"""
@file src/core/security_headers.py
@description Adds a fixed set of security headers to every HTTP response,
including error responses. Registered as the outermost application
middleware (see main.py -- it must be the FIRST call to app.add_middleware,
since Starlette wraps middleware in the reverse of registration order, so
the first one added ends up outermost and therefore sees every response,
including ones that never reach the route body).

Implemented as a raw ASGI middleware rather than Starlette's
BaseHTTPMiddleware: BaseHTTPMiddleware buffers the whole response to let
`dispatch` inspect it, which breaks streaming responses and adds latency.
Wrapping `send` directly adds headers to the `http.response.start` message
with neither cost.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _build_csp(supabase_origin: str) -> str:
    """
    `img-src`/`media-src` include the Supabase storage origin because media
    (photos, audio) is served from signed Supabase URLs, not self-hosted --
    this is the one place the policy has to reach off-origin, and it is
    scoped to exactly that origin, not a wildcard.
    """
    img_src = "'self' blob: data:" + (f" {supabase_origin}" if supabase_origin else "")
    media_src = "'self'" + (f" {supabase_origin}" if supabase_origin else "")
    return "; ".join(
        [
            "default-src 'self'",
            f"img-src {img_src}",
            f"media-src {media_src}",
            "font-src 'self' fonts.gstatic.com",
            "style-src 'self' fonts.googleapis.com",
            "script-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'self'",
        ]
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, supabase_origin: str = "") -> None:
        self.app = app
        csp = _build_csp(supabase_origin)
        # Static per-process -- none of these values vary per request, so
        # they're built once rather than on every response.
        self._headers: list[tuple[bytes, bytes]] = [
            (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            # microphone is self because the product records voice; camera
            # and geolocation are never used anywhere in this product.
            (b"permissions-policy", b"camera=(), microphone=(self), geolocation=()"),
            (b"content-security-policy", csp.encode("ascii")),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self._headers)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
