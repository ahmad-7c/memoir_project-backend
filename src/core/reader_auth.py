"""
@file src/core/reader_auth.py
@description Authentication for readers: anonymous relatives who unlock a share link
with a display name + password instead of holding an account. Deliberately kept
separate from src/core/auth.py (owner authentication) — a reader has no account and
nothing to escalate to, so the two verification paths never share a code branch.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Header, HTTPException, status
from pydantic import BaseModel

from src.core.config import settings

# Signed with our own secret, never the Supabase JWKS material — this token type
# has nothing to do with owner accounts.
READER_TOKEN_ALGORITHM = "HS256"
READER_TOKEN_TTL_SECONDS = 24 * 60 * 60
READER_TOKEN_TYPE = "reader"


class ShareContext(BaseModel):
    """Everything a reader-authenticated request is allowed to know about itself."""
    share_link_id: str
    memoir_id: str
    display_name: str
    can_comment: bool


def issue_reader_token(share_link_id: str, memoir_id: str, display_name: str, can_comment: bool) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "share_link_id": str(share_link_id),
        "memoir_id": str(memoir_id),
        "display_name": display_name,
        "can_comment": bool(can_comment),
        "type": READER_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(seconds=READER_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.reader_jwt_secret, algorithm=READER_TOKEN_ALGORITHM)


def _context_from_payload(payload: dict) -> ShareContext:
    return ShareContext(
        share_link_id=payload["share_link_id"],
        memoir_id=payload["memoir_id"],
        display_name=payload["display_name"],
        can_comment=bool(payload.get("can_comment", False)),
    )


def decode_reader_token(token: str) -> ShareContext:
    """Strictly decodes a reader token. Raises 401 on anything wrong with it."""
    try:
        payload = jwt.decode(token, settings.reader_jwt_secret, algorithms=[READER_TOKEN_ALGORITHM])
        if payload.get("type") != READER_TOKEN_TYPE:
            raise jwt.InvalidTokenError("Not a reader token")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please enter the password again."
        )
    return _context_from_payload(payload)


def try_decode_reader_token(token: Optional[str]) -> Optional[ShareContext]:
    """Non-raising variant for endpoints that accept EITHER a reader or an owner token."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.reader_jwt_secret, algorithms=[READER_TOKEN_ALGORITHM])
        if payload.get("type") != READER_TOKEN_TYPE:
            return None
    except jwt.PyJWTError:
        return None
    return _context_from_payload(payload)


def is_expired_or_malformed_reader_token(token: Optional[str]) -> bool:
    """
    True if `token` is signature-valid as one of OUR reader tokens but rejected only
    because it's expired (or otherwise no longer current). Used by dual-mode
    (reader-or-owner) endpoints to tell "your reader session died, re-enter the
    password" apart from "this isn't a reader token at all, try it as an owner token".
    """
    if not token:
        return False
    try:
        jwt.decode(
            token,
            settings.reader_jwt_secret,
            algorithms=[READER_TOKEN_ALGORITHM],
            options={"verify_exp": False},
        )
        return True
    except jwt.PyJWTError:
        return False


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.split(" ", 1)[1].strip() or None


def get_share_context(authorization: Optional[str] = Header(None)) -> ShareContext:
    """
    FastAPI dependency for routes that require reader access specifically (e.g.
    posting a comment as a reader). Resolution order:
      1. Missing/invalid/expired reader token -> 401 (frontend re-prompts for password).
      2. Otherwise -> the decoded ShareContext.
    Link revocation / visibility / publish-state checks happen where the memoir_id
    is resolved against the database, not here — a token can outlive those changes.
    """
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Reader session required. Please enter the password again."
        )
    return decode_reader_token(token)
