"""
@file api/memoir.py
@description FastAPI router handling HTTP endpoints for memoir creation and management.
"""

from fastapi import APIRouter, Depends, status
from src.schemas.memoir import MemoirCreateRequest, MemoirResponseEnvelope
from src.domain.memoir_service import MemoirService
from src.core.auth import get_current_user

router = APIRouter(prefix="/api/memoirs", tags=["Memoirs"])

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=MemoirResponseEnvelope)
def create_memoir(
    payload: MemoirCreateRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Creates a new root memoir container for a subject. 
    This must be executed first to obtain a memoir_id before adding media or memories.
    """
    # extract user ID string from the dictionary
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    
    user_session = {
        "user_id": user_id,
        "email": current_user.get("email"),
        "claims": current_user.get("claims"),
    }
    new_memoir = MemoirService.create_memoir(payload, user_session)
    return {
        "success": True,
        "message": "Memoir successfully created.",
        "data": new_memoir
    }


@router.get("/", status_code=status.HTTP_200_OK)
def list_my_memoirs(current_user: dict = Depends(get_current_user)):
    """
    Lists every memoir the caller actively participates in. The frontend
    needs this on login to tell a returning user with an existing memoir
    apart from a brand-new user who still needs onboarding -- there was no
    way to ask that before this endpoint existed.
    """
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    memoirs = MemoirService.list_user_memoirs(user_id)
    return {"success": True, "data": memoirs}


@router.post("/{memoir_id}/publish", status_code=status.HTTP_200_OK)
def publish_memoir(memoir_id: str, current_user: dict = Depends(get_current_user)):
    """
    Owner-only. Publishing makes the memoir shareable (share-link creation
    requires it) and, per the immutability trigger, locks its content from
    further edits. Idempotent -- publishing an already-published memoir just
    returns its current state.
    """
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    memoir = MemoirService.publish_memoir(memoir_id, user_id)
    return {"success": True, "message": "Memoir published.", "data": memoir}