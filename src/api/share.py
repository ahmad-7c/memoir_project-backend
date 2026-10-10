from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from src.core.config import ACCESS_TOKEN_COOKIE_NAME, settings
from src.core.auth import get_current_user
from src.core.reader_auth import ShareContext
from src.domain.access_control import resolve_memoir_access
from src.domain.reader_access import get_share_context
from src.domain.share_service import ShareService
from src.integrations.comments_repository import CommentsRepository
from src.integrations import narrative_repository
from src.integrations.share_repository import ShareRepository
from src.schemas.comments import CommentCreate, CommentResponse
from src.schemas.narrative import NarrativeSourcesResponse, SourceMediaResponse, SourceMemoryResponse
from src.schemas.share import (
    ShareLinkResponse, ShareLinkResponseEnvelope, ShareLinkUpdateRequest,
    SharedChapterResponse, SharedMemoirResponse, SharedMemoirResponseEnvelope,
    SharedMediaAssetResponse, SharedMemoryResponse, SharedNarrativeSectionResponse,
    UnlockRequest, UnlockResponse, UnlockResponseEnvelope,
)

owner_router = APIRouter(prefix="/api/memoirs", tags=["Share Links"])
reader_router = APIRouter(prefix="/api/share", tags=["Shared Memoirs"])

async def _to_link_response(link: Dict[str, Any]) -> ShareLinkResponse:
    memoir = await ShareRepository.get_memoir_by_id(str(link["memoir_id"]))
    can_comment = bool(memoir) and memoir.get("comment_policy") == "anyone_who_can_view"
    return ShareLinkResponse(
        id=str(link["id"]),
        memoir_id=str(link["memoir_id"]),
        scope=link["scope"],
        token=link["token"],
        url=f"{settings.share_link_base_url.rstrip('/')}/{link['token']}",
        visibility=link.get("visibility") or "password",
        has_password=bool(link.get("password_hash")),
        can_comment=can_comment,
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
    return {"success": True, "message": "Share link ready.", "data": await _to_link_response(link)}

@owner_router.get("/{memoir_id}/share-link", response_model=ShareLinkResponseEnvelope)
async def get_share_link(memoir_id: str, current_user: dict = Depends(get_current_user)):
    """Read-only retrieval for the owner's share panel -- never creates one."""
    user_id = str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))
    link = await ShareService.get_share_link(memoir_id, user_id)
    return {"success": True, "message": "Operation successful", "data": await _to_link_response(link)}

@owner_router.patch("/{memoir_id}/share-link", response_model=ShareLinkResponseEnvelope)
async def patch_share_link(memoir_id: str, payload: ShareLinkUpdateRequest, current_user: dict = Depends(get_current_user)):
    user_id = str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))
    link = await ShareService.update_share_link(memoir_id, user_id, payload)
    return {"success": True, "message": "Share link updated.", "data": await _to_link_response(link)}

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
async def read_shared_memoir(token: str, request: Request, authorization: Optional[str] = Header(None)):
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
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE_NAME),
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
        can_comment=memoir.get("comment_policy") == "anyone_who_can_view",
        memories=memories,
    )
    return {"success": True, "message": "Operation successful", "data": data}


# ---------------------------------------------------------------------------
# NEW reader-only routes. Every one of these depends on get_share_context
# (domain/reader_access.py) ALONE -- never resolve_memoir_access, never
# get_current_user. That is the point: a reader has no account and nothing
# to escalate to, so this code path must never be able to accidentally
# authenticate as an owner, and the owner's code path must never be able to
# accidentally authenticate as a reader. See reader_access.py's docstring.
# ---------------------------------------------------------------------------


@reader_router.get("/{token}/memoir", response_model=SharedMemoirResponseEnvelope)
async def get_shared_memoir_full(token: str, share: ShareContext = Depends(get_share_context)):
    """
    Everything the reader's memoir page renders, in a constant number of
    queries regardless of memory count -- see
    ShareRepository.get_full_memoir_view.
    """
    memoir = await ShareRepository.get_memoir_by_id(share.memoir_id)
    if not memoir:
        # get_share_context already re-validated this memoir is published and
        # live; a miss here would mean it vanished between those two calls.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer available.")

    view = await ShareRepository.get_full_memoir_view(share.memoir_id)

    data = SharedMemoirResponse(
        id=str(memoir["id"]),
        subject_name=memoir.get("subject_name"),
        subject_born_on=memoir.get("subject_born_on"),
        subject_died_on=memoir.get("subject_died_on"),
        subject_is_living=bool(memoir.get("subject_is_living")),
        description=memoir.get("description"),
        can_comment=share.can_comment,
        chapters=[
            SharedChapterResponse(
                id=str(c["id"]), title=c["title"], summary=c.get("summary"), sort_order=c["sort_order"]
            )
            for c in view["chapters"]
        ],
        narrative_sections=[
            SharedNarrativeSectionResponse(
                id=str(s["id"]),
                chapter_id=str(s["chapter_id"]) if s.get("chapter_id") else None,
                position=s["position"],
                body=s["body"],
                source_memory_ids=[str(m) for m in s.get("source_memory_ids", [])],
            )
            for s in view["narrative_sections"]
        ],
        memories=[
            SharedMemoryResponse(
                id=str(m["id"]),
                title=m.get("title"),
                body_text=m.get("body_text"),
                occurred_start=m.get("occurred_start"),
                occurred_end=m.get("occurred_end"),
                occurred_precision=m.get("occurred_precision"),
                chapter_id=str(m["chapter_id"]) if m.get("chapter_id") else None,
                created_at=m["created_at"],
                media=[
                    SharedMediaAssetResponse(
                        id=str(a["id"]),
                        kind=a.get("kind"),
                        mime_type=a.get("mime_type"),
                        caption=a.get("caption"),
                        duration_ms=a.get("duration_ms"),
                        width_px=a.get("width_px"),
                        height_px=a.get("height_px"),
                        playback_url=a.get("playback_url"),
                        transcript_text=a.get("transcript_text"),
                    )
                    for a in m.get("media", [])
                ],
            )
            for m in view["memories"]
        ],
    )
    return {"success": True, "message": "Operation successful", "data": data}


@reader_router.get("/{token}/memories/{section_id}/sources", response_model=NarrativeSourcesResponse)
async def get_shared_memoir_sources(
    token: str, section_id: str, share: ShareContext = Depends(get_share_context)
):
    """The original memories behind one narrative section, verbatim."""
    section = narrative_repository.fetch_narrative_section(section_id, share.memoir_id)
    memories = narrative_repository.fetch_source_memories(share.memoir_id, section["source_memory_ids"])

    return NarrativeSourcesResponse(
        section_id=section_id,
        memories=[
            SourceMemoryResponse(
                id=str(m["id"]),
                title=m.get("title"),
                body_text=m.get("body_text"),
                occurred_start=str(m["occurred_start"]) if m.get("occurred_start") else None,
                author_name=m.get("author_name"),
                media=[
                    SourceMediaResponse(
                        id=str(media["id"]),
                        kind=media.get("kind"),
                        mime_type=media.get("mime_type"),
                        caption=media.get("caption"),
                        duration_ms=media.get("duration_ms"),
                        width_px=media.get("width_px"),
                        height_px=media.get("height_px"),
                        playback_url=media.get("playback_url"),
                        transcript_text=media.get("transcript_text"),
                    )
                    for media in m.get("media", [])
                ],
            )
            for m in memories
        ],
    )


@reader_router.get("/{token}/comments", response_model=List[CommentResponse])
async def list_shared_comments(
    token: str,
    memory_id: Optional[str] = Query(None),
    narrative_section_id: Optional[str] = Query(None),
    share: ShareContext = Depends(get_share_context),
):
    """
    Read is allowed regardless of `can_comment` -- that flag gates POSTING,
    not reading what others already said (the owner can disable new
    comments without hiding the conversation that already happened).
    """
    if bool(memory_id) == bool(narrative_section_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of memory_id or narrative_section_id.",
        )

    # Confirms the target actually belongs to THIS memoir before returning
    # anything -- memory_id/narrative_section_id are caller-supplied, and
    # share.memoir_id is the only thing resolve_live_link has verified.
    if memory_id:
        context = await CommentsRepository.get_memory_memoir_context(memory_id)
    else:
        context = await CommentsRepository.get_narrative_section_memoir_context(narrative_section_id)
    if not context or str(context["memoir_id"]) != share.memoir_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    return await CommentsRepository.get_comments(
        memory_id=memory_id, narrative_section_id=narrative_section_id
    )


@reader_router.post("/{token}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_shared_comment(
    token: str, payload: CommentCreate, share: ShareContext = Depends(get_share_context)
):
    """
    THE rule this whole feature hangs on: the author's name is `share.
    display_name`, read from the signed reader token that `get_share_context`
    just re-verified -- never from `payload`. Nothing here even looks at a
    name field on the request body.
    """
    if not payload.body or not payload.body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Comment text cannot be empty.")

    if not share.can_comment:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Commenting is not enabled for this memoir.")

    if bool(payload.memory_id) == bool(payload.narrative_section_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A comment must target exactly one memory or narrative section.",
        )

    if payload.memory_id:
        context = await CommentsRepository.get_memory_memoir_context(payload.memory_id)
    else:
        context = await CommentsRepository.get_narrative_section_memoir_context(payload.narrative_section_id)
    if not context or str(context["memoir_id"]) != share.memoir_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    insert_payload = payload.dict()
    insert_payload["memoir_id"] = share.memoir_id
    return await CommentsRepository.insert_reader_comment(insert_payload, share)
