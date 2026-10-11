"""
@file src/core/rate_limit.py
@description In-memory fixed-window rate limiting, applied via FastAPI
dependencies rather than global middleware -- the limits differ per route
(token, IP, or user) in ways a single generic middleware can't express
cleanly.

Single-process, in-memory by design: this deployment is one Lightsail
instance (see AGENTS.md / deploy notes), not a multi-instance setup behind a
load balancer. A shared store (Redis) would be the right call the moment a
second instance exists, since counts would otherwise be split across
instances and the effective limit would silently multiply -- documented
limitation, not an oversight. Safe under asyncio: the check-then-increment
below contains no `await`, so no other request's coroutine can interleave
with it on a single event loop.
"""

import time
from collections import defaultdict
from fastapi import Depends, HTTPException, Request, status
from src.core.auth import get_current_user


class _FixedWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        window_start = now - window_seconds
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.pop(0)
        if len(hits) >= limit:
            retry_after = max(1, int(hits[0] + window_seconds - now) + 1)
            return False, retry_after
        hits.append(now)
        return True, 0


_limiter = _FixedWindowLimiter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce(key: str, limit: int, window_seconds: int) -> None:
    allowed, retry_after = _limiter.check(key, limit, window_seconds)
    if not allowed:
        # Deliberately generic: no remaining-attempts count, no mention of
        # which limit (token vs IP) was hit -- that information helps an
        # attacker tune their request rate, not a legitimate user.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit_unlock(request: Request, token: str) -> None:
    """
    Reader unlock (POST /api/share/{token}/unlock): a password-guessing
    endpoint with no account lockout concept, so it is the one place in this
    API a brute force is realistic. Two independent limits -- per-token (an
    attacker who finds one link can't grind its password) and per-IP (an
    attacker can't spread guesses across many links from one machine to
    dodge the per-token limit).
    """
    _enforce(f"unlock-token:{token}", limit=5, window_seconds=900)
    _enforce(f"unlock-ip:{_client_ip(request)}", limit=20, window_seconds=900)


def rate_limit_auth(request: Request) -> None:
    """Login/signup, per-IP. Generous enough for a real user mistyping a
    password a few times, tight enough to blunt credential stuffing."""
    _enforce(f"auth-ip:{_client_ip(request)}", limit=10, window_seconds=900)


def rate_limit_upload_url(current_user_id: str = Depends(get_current_user)) -> None:
    """
    Media presigned-url requests, per-user. FastAPI caches a dependency's
    result per request by default (same callable, same request), so the
    route's own `Depends(get_current_user)` and this one resolve to a
    single call, not two.
    """
    _enforce(f"upload-url-user:{current_user_id}", limit=30, window_seconds=60)
