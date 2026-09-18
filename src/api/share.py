from datetime import datetime, timezone
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status

from src.core.config import settings
from src.core.auth import get_current_user
from src.domain.access_control import resolve_memoir_access
from src.domain.share_service import ShareService
from src.integrations.share_repository import ShareRepository
from src.schemas.share import (
    ShareLinkResponse, ShareLinkResponseEnvelope, ShareLinkUpdateRequest,
    SharedMemoirResponse, SharedMemoirResponseEnvelope,
    UnlockRequest, UnlockResponse, UnlockResponseEnvelope,
)

owner_router = APIRouter(prefix="/api/memoirs", tags=["Share Links"])
reader_router = APIRouter(prefix="/api/share", tags=["Shared Memoirs"])

def _to_link_response(link: Dict[str, Any]) -> ShareLinkResponse:
    return ShareLinkResponse(
        id=str(link["id"]),
        memoir_id=str(link["memoir_id"]),
        scope=link["scope"],
        token=link["token"],
        url=f"{settings.share_link_base_url.rstrip('/')}/{link['token']}",
        visibility=link.get("visibility") or "password",
        has_password=bool(link.get("password_hash")),
        created_by_participant_id=str(link["created_by_participant_id"]) if link.get("created_by_participant_id") else None,
        created_at=link["created_at"],
        expires_at=link.get("expires_at"),
        revoked_at=link.get("revoked_at"),
        open_count=link.get("open_count", 0)
    )

# --- OWNER ROUTES ---
@owner_router.post("/{memoir_id}/share-link", status_code=201, response_model=ShareLinkResponseEnvelope)
async def create_share_link(memoir_id: str, current_user: dict = Depends(get_current_user)):
    user_id = str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))
    link = await ShareService.create_or_get_share_link(memoir_id, user_id)
    return {"success": True, "message": "Share link ready.", "data": _to_link_response(link)}

@owner_router.patch("/{memoir_id}/share-link", response_model=ShareLinkResponseEnvelope)
async def patch_share_link(memoir_id: str, payload: ShareLinkUpdateRequest, current_user: dict = Depends(get_current_user)):
    user_id = str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))
    link = await ShareService.update_share_link(memoir_id, user_id, payload)
    return {"success": True, "message": "Share link updated.", "data": _to_link_response(link)}

@owner_router.delete("/{memoir_id}/share-link", status_code=200)
async def delete_share_link(memoir_id: str, current_user: dict = Depends(get_current_user)):
    user_id = str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))
    await ShareService.revoke_share_link(memoir_id, user_id)
    return {"success": True, "message": "Share link revoked."}

# --- READER ROUTES ---

@reader_router.post("/{token}/unlock", response_model=UnlockResponseEnvelope)
async def unlock_shared_memoir(token: str, payload: UnlockRequest):
    """
    A reader identifies themselves with a name + the password the owner shared
    personally, and gets back a short-lived signed reader token to use for every
    subsequent request (reading the memoir, reading/posting comments).
    """
    result = await ShareService.unlock_share_link(token, payload.display_name, payload.password)
    return {"success": True, "message": "Unlocked.", "data": UnlockResponse(**result)}

@reader_router.get("/{token}", response_model=SharedMemoirResponseEnvelope)
async def read_shared_memoir(token: str, authorization: Optional[str] = Header(None)):
    link = await ShareRepository.get_link_by_token(token)

    if not link or link.get("revoked_at") or link.get("visibility") == "private":
        raise HTTPException(status_code=404, detail="Not found.")

    if link.get("expires_at"):
        expires_at = datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=404, detail="Link expired.")

    memoir = await ShareService.get_published_memoir(link["memoir_id"])
    if not memoir:
        raise HTTPException(status_code=404, detail="Not found.")

    # Accept EITHER a reader token issued by /unlock for this exact link, OR an
    # owner JWT for an active participant of this memoir (e.g. previewing their own
    # share page). Missing/invalid credentials -> 401, so the frontend can tell
    # "please unlock again" apart from "this link is dead" (404, handled above).
    resolve_memoir_access(
        memoir_id=str(link["memoir_id"]),
        authorization=authorization,
        expected_share_link_id=str(link["id"]),
        unauthenticated_status=status.HTTP_401_UNAUTHORIZED,
    )

    await ShareRepository.increment_open_count(link["id"], link.get("open_count", 0))

    memories = await ShareRepository.get_shared_memoir_view(link["memoir_id"])

    data = SharedMemoirResponse(
        id=str(memoir["id"]),
        subject_name=memoir.get("subject_name"),
        subject_born_on=memoir.get("subject_born_on"),
        subject_died_on=memoir.get("subject_died_on"),
        subject_is_living=bool(memoir.get("subject_is_living")),
        description=memoir.get("description"),
        can_comment=memoir.get("comment_policy") == "public",
        memories=memories,
    )
    return {"success": True, "message": "Operation successful", "data": data}
