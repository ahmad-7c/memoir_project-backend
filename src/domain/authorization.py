"""
@file src/domain/authorization.py
@description Centralized authorization and role-checking helpers for memoir participants.
"""

from fastapi import HTTPException, status
from src.integrations import memoir_repository, participant_repository


def verify_active_participant(memoir_id: str, user_id: str, required_roles: list[str] = None) -> dict:
    """
    Verifies that a user is an active (non-removed) participant of a memoir 
    and optionally checks if they possess one of the required roles.

    Args:
        memoir_id (str): The memoir container ID.
        user_id (str): The user ID.
        required_roles (list[str], optional): List of allowed roles (e.g., ['owner', 'admin', 'contributor']).

    Returns:
        dict: The participant record dictionary.

    Raises:
        HTTPException (403): If unauthorized, removed, or lacking the required role.
    """
    if isinstance(user_id, dict):
        user_id = user_id.get("user_id") or user_id.get("id") 

    # Ensure it's explicitly a clean string
    user_id_str = str(user_id)
    
    res = participant_repository.fetch_participant(str(memoir_id), user_id_str)
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: You are not an active participant of this memoir."
        )

    participant = res.data[0]
    
    # 🔍 ADD THIS LINE TO INSPECT WHAT PYTHON ACTUALLY SEES
    print("DEBUG PARTICIPANT FETCHED FROM DB:", participant)
    
    # Explicit check for removed status (just in case query soft-filter is bypassed)
    if participant.get("removed_at") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your participation in this memoir has been revoked."
        )

    # If specific write/admin roles are required, enforce them
    if required_roles:
        user_role = participant.get("role")
        print(f"DEBUG USER ROLE: '{user_role}' (Type: {type(user_role)})") # Check value & type
        if user_role not in required_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Requires one of the following roles: {', '.join(required_roles)}."
            )

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