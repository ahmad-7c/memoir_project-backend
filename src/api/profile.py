"""
@file api/profile.py
@description FastAPI router for the authenticated owner's profile page.
"""

from fastapi import APIRouter, Depends, status

from src.core.auth import get_current_user
from src.domain.profile_service import ProfileService
from src.schemas.profile import OwnerProfileResponseEnvelope

router = APIRouter(prefix="/api/me", tags=["Profile"])


@router.get("/profile", status_code=status.HTTP_200_OK, response_model=OwnerProfileResponseEnvelope)
def get_owner_profile(current_user: dict = Depends(get_current_user)):
    """
    Account details plus every memoir the caller owns, drafts first (newest
    edit first), then published (newest publish first).
    """
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    data = ProfileService.get_owner_profile(user_id)
    return {"success": True, "message": "Operation successful", "data": data}
