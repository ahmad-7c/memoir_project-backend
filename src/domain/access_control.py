"""
@file src/domain/access_control.py
@description Shared "is this caller allowed to see this memoir" resolution. Several
endpoints (reading a shared memoir, listing/posting comments) accept EITHER an owner
JWT with verified ownership OR a valid reader (share-link) token — this is the one
place that logic lives, so the two rules can't drift apart.

This does NOT merge owner and reader identity into a single trust boundary: it tries
each verification path independently (src.core.auth for owners, src.core.reader_auth
for readers) and returns which one succeeded. Neither path can influence the other.
"""

from dataclasses import dataclass
from typing import Literal, Optional

from fastapi import HTTPException, status

from src.core.auth import decode_owner_jwt
from src.core.reader_auth import (
    ShareContext,
    extract_bearer_token,
    is_expired_or_malformed_reader_token,
    try_decode_reader_token,
)
from src.integrations import participant_repository


@dataclass
class AccessResult:
    kind: Literal["owner", "reader"]
    user_id: Optional[str] = None
    share_context: Optional[ShareContext] = None


def resolve_memoir_access(
    memoir_id: str,
    authorization: Optional[str],
    expected_share_link_id: Optional[str] = None,
    unauthenticated_status: int = status.HTTP_404_NOT_FOUND,
) -> AccessResult:
    """
    Grants access if the bearer token is EITHER a valid reader token scoped to this
    memoir (and, when given, this exact share link) OR an owner JWT belonging to an
    active participant of this memoir. Raises HTTPException otherwise.

    `unauthenticated_status` controls the status for a missing/unusable credential:
    404 (default) for routes that must not reveal whether the resource exists, or 401
    for routes where a reader token is the expected credential and the frontend needs
    to distinguish "please re-enter the password" from "this link is dead".
    """
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=unauthenticated_status, detail="Not found.")

    reader_ctx = try_decode_reader_token(token)
    if reader_ctx:
        if str(reader_ctx.memoir_id) != str(memoir_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
        if expected_share_link_id and reader_ctx.share_link_id != str(expected_share_link_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
        return AccessResult(kind="reader", share_context=reader_ctx)

    if is_expired_or_malformed_reader_token(token):
        # This is unambiguously one of our reader tokens, just no longer valid.
        # Never fall through to trying it as an owner token.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please enter the password again.",
        )

    try:
        owner = decode_owner_jwt(token)
    except HTTPException:
        raise HTTPException(status_code=unauthenticated_status, detail="Not found.")

    user_id = owner.get("user_id")
    participant_res = participant_repository.fetch_participant(memoir_id, user_id)
    if not participant_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    return AccessResult(kind="owner", user_id=user_id)
