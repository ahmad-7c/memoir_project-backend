"""
@file src/core/auth.py
@description FastAPI security dependency for high-performance local JWT verification
via Supabase JWKS, eliminating remote auth network round-trips and handling exceptions cleanly.
"""

from typing import Optional

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Request, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from src.core.config import ACCESS_TOKEN_COOKIE_NAME, SUPABASE_JWKS_URL, settings

# auto_error=False: a browser client won't send an Authorization header at all
# (the token lives in the httpOnly cookie instead) -- HTTPBearer's default
# behavior of raising 403 on a missing header would reject every real request
# before get_current_user ever gets a chance to check the cookie.
security = HTTPBearer(auto_error=False)

# Verified against this project's live JWKS endpoint (both current signing keys are
# kty=EC / crv=P-256, i.e. ES256 — see auth/v1/.well-known/jwks.json). Hardcoded to
# exactly that. Accepting HS256 alongside a JWKS-sourced key would let anyone sign
# their own token using that PUBLIC key as an HMAC secret — a full auth bypass — so
# nothing else is accepted even as a fallback. If this project's signing key is ever
# rotated to a different algorithm, this constant must be updated to match.
ALLOWED_JWT_ALGORITHMS = {"ES256"}

# Initialize the PyJWKClient to fetch and cache public signing keys from Supabase
jwks_client = PyJWKClient(SUPABASE_JWKS_URL) if SUPABASE_JWKS_URL else None

SUPABASE_ISSUER = f"{settings.supabase_url.rstrip('/')}/auth/v1"


def decode_owner_jwt(token: str) -> dict:
    """
    Validates a Supabase-issued Bearer JWT locally against the project's JWKS endpoint
    without triggering a remote network round-trip to Supabase Auth on every request.
    """
    try:
        if not jwks_client:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_JWKS_URL is not configured for local JWT validation."
            )

        # Fetch matching signing key and decode/verify token locally
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        # The algorithm is derived from the JWK itself (its "alg", or its "kty"/"crv"),
        # never from the token's own header. Reject anything outside the asymmetric
        # allowlist instead of trusting whatever the caller asked for.
        algorithm = signing_key.algorithm_name
        if algorithm not in ALLOWED_JWT_ALGORITHMS:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unsupported token signing algorithm."
            )

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            audience="authenticated",
            issuer=SUPABASE_ISSUER,
            # A few seconds of clock drift between this server and Supabase's
            # auth server is normal, not an attack -- with zero leeway (PyJWT's
            # default) a token can be rejected as "not yet valid (iat)"
            # immediately after login purely from that drift. Real 401s from
            # an actually-expired or forged token still fail past this window.
            leeway=10,
            options={"require": ["exp", "iss", "aud", "sub"]}
        )

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload: missing subject identifier (sub)."
            )

        return {
            "user_id": user_id,
            "email": payload.get("email"),
            "role": payload.get("role"),
            "claims": payload
        }

    except HTTPException:
        # CRITICAL: Re-raise HTTPExceptions directly so status codes (e.g. 401) aren't swallowed or re-wrapped
        raise
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency wrapper around decode_owner_jwt for owner-authenticated
    routes. Prefers the httpOnly cookie set at login (the only thing a
    browser client uses -- frontend JS never sees this token at all), and
    falls back to an Authorization: Bearer header for non-browser callers
    (scripts, tests, future API clients) that were never issued a cookie.
    """
    token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if not token and credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    return decode_owner_jwt(token)
