"""
@file domain/reader_access.py
@description The reader-only authentication dependency for every
`/api/share/{token}/...` route.

DELIBERATELY NOT a branch inside the owner's `get_current_user`. A reader has
no account and nothing to escalate to; a single dependency that tries "is
this an owner JWT? ... else is it a reader JWT?" is exactly the shape that
lets a bug in one branch leak privilege into the other. This file knows
nothing about Supabase JWTs, owner participants, or `get_current_user` at
all -- it only ever resolves a reader token against a share link.

Two independent failure classes, deliberately not collapsed into one status
code (the frontend branches on this):

  401 -- the reader credential itself is the problem (missing, malformed,
         expired, wrong link). Re-show the password form.
  404 -- the credential was fine, but the underlying share link is no
         longer usable (revoked, made private, or the memoir is gone/
         unpublished). "This link no longer works" -- a different screen.

The 404 path is NOT just a check made once at /unlock. `ShareService.
resolve_live_link` re-queries the memoir_link row on every single request
through this dependency, so revoking a link takes effect on the reader's
very next request rather than waiting for their already-issued 24h JWT to
expire on its own.
"""

from typing import Optional

import jwt
from fastapi import Header, HTTPException, Path, status

from src.core.config import settings
from src.core.reader_auth import READER_TOKEN_ALGORITHM, READER_TOKEN_TYPE, ShareContext, extract_bearer_token
from src.domain.share_service import ShareService


async def get_share_context(
    token: str = Path(..., description="The share link's own URL token."),
    authorization: Optional[str] = Header(None),
) -> ShareContext:
    """
    FastAPI dependency for reader-facing routes. `token` is the share link's
    own token (the one in the URL the family was given); the reader's signed
    session JWT travels separately as `Authorization: Bearer <reader_token>`,
    issued by POST /api/share/{token}/unlock.
    """
    reader_jwt = extract_bearer_token(authorization)
    if not reader_jwt:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Please enter the password to open this memoir.",
        )

    try:
        payload = jwt.decode(reader_jwt, settings.reader_jwt_secret, algorithms=[READER_TOKEN_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please enter the password again.",
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session. Please enter the password again.",
        )

    if payload.get("type") != READER_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session. Please enter the password again.",
        )

    # Live re-check, not just "is the signature valid" -- see module
    # docstring. Not-found/revoked/private/unpublished are all the same 404
    # from here, matching unlock's own behaviour.
    resolved = await ShareService.resolve_live_link(token)
    link = resolved["link"]
    memoir = resolved["memoir"]

    if str(link["id"]) != str(payload.get("share_link_id")):
        # A reader token minted for a DIFFERENT share link is being replayed
        # against this one's URL. Never trust path/token cross-talk.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer available.")

    display_name = payload.get("display_name")
    if not display_name:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session. Please enter the password again.",
        )

    # Computed live from the memoir's CURRENT comment_policy, not the JWT's
    # snapshot from unlock time -- the owner can turn commenting off after a
    # reader has already unlocked, and that must take effect immediately.
    can_comment = memoir.get("comment_policy") == "anyone_who_can_view"

    return ShareContext(
        share_link_id=str(link["id"]),
        memoir_id=str(link["memoir_id"]),
        display_name=display_name,
        can_comment=can_comment,
    )
