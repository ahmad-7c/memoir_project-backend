"""
@file src/domain/authorization.py
@description Centralized authorization and role-checking helpers for memoir participants.

This is the single shared decision point for "may this caller touch this
memoir?". Every route that answers that question calls in here, which is the
whole reason it exists: copy-pasted permission logic drifts, and drift in
permission logic is a security bug that ships.
"""

import logging
from typing import Any, List, Optional

from fastapi import HTTPException, status
from src.integrations import memoir_repository, participant_repository

logger = logging.getLogger(__name__)

# One message for every denial, regardless of which check failed.
#
# The previous version returned 403 with a message that named the specific
# reason ("You are not an active participant of this memoir" vs "Requires one of
# the following roles: owner, admin"). Two problems with that. A 403 confirms
# the memoir exists, which turns any id endpoint into an enumeration oracle: a
# caller can distinguish "wrong tenant" from "does not exist" and walk the id
# space. And a role-specific message tells a non-participant what the role set
# is, which is reconnaissance for the next attempt.
#
# So: 404, and the same detail as every other not-found in this codebase. The
# owner gets nothing more than the fact that there is nothing here for them.
_NOT_FOUND = "Memoir not found."


def verify_active_participant(
    memoir_id: str,
    user_id: Any,
    required_roles: Optional[List[str]] = None,
) -> dict:
    """
    Verifies that a user is an active (non-removed) participant of a memoir
    and optionally checks if they possess one of the required roles.

    Args:
        memoir_id (str): The memoir container ID.
        user_id: The user ID. A dict is accepted because `Depends(get_current_user)`
            returns one, and resolving it here is the point.
        required_roles (list[str], optional): List of allowed roles
            (e.g., ['owner', 'admin', 'contributor']).

    Returns:
        dict: The participant record.

    Raises:
        HTTPException (404): If the caller is not an active participant, has been
            removed, or lacks a required role. Never 403 — see the note above.
    """
    if isinstance(user_id, dict):
        user_id = user_id.get("user_id") or user_id.get("id") or user_id.get("sub")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not resolve the authenticated user.",
        )

    res = participant_repository.fetch_participant(str(memoir_id), str(user_id))
    if not res.data:
        # No participant row. Either the memoir does not exist or the caller is
        # not in it; the caller cannot tell which, and must not be able to.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    participant = res.data[0]

    # Explicit re-check rather than trusting the query's soft-delete filter. The
    # repository already excludes removed participants, but this is the one place
    # that decides whether a removed collaborator can still read a memoir, so it
    # does not depend on a filter three files away staying correct.
    if participant.get("removed_at") is not None:
        # Logged at info with no participant payload: a removal being acted on is
        # worth a trail, and the record itself is not. The previous version
        # printed the whole participant row to stdout on every authorized call.
        logger.info(
            "rejected access: participant removed (memoir_id=%s)", str(memoir_id)
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    if required_roles:
        user_role = participant.get("role")
        if user_role not in required_roles:
            logger.info(
                "rejected access: role %r not in required set (memoir_id=%s)",
                user_role,
                str(memoir_id),
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    return participant


def assert_memoir_editable(memoir_id: str) -> None:
    """
    A published memoir is immutable (Feature Request 04). Comments are the only
    thing that may still be added after publication — every other write path to
    memoir content (memories, media, transcripts) must call this first.

    This is layer 1 of two: a database trigger (see migrations/) is the backstop
    for writes that don't go through this function.

    Raises:
        HTTPException (409): If the memoir's status is 'published'.
    """
    res = memoir_repository.fetch_memoir_status(str(memoir_id))
    memoir = res.data[0] if res.data else None
    if memoir and memoir.get("status") == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This memoir has been published and can no longer be changed."
        )