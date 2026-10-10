"""
@file api/narrative.py
@description Routes for the AI-composed biographical narrative: generation,
owner review/edit/delete/regenerate, and the Sources expand.

Generation and editing are owner-only and blocked on a published memoir
(assert_memoir_editable). Reading the narrative and its sources is owner OR
a reader with a valid unlocked share token -- the same dual-access rule
already used for comments and the shared memoir view (access_control.py).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from typing import Optional

from src.core.auth import get_current_user
from src.core.config import ACCESS_TOKEN_COOKIE_NAME
from src.domain import narrative_service
from src.domain.access_control import resolve_memoir_access
from src.domain.authorization import assert_memoir_editable
from src.integrations import narrative_repository as repo
from src.schemas.narrative import (
    GenerateNarrativeResponseEnvelope,
    MarkReviewedResponseEnvelope,
    NarrativeSectionEnvelope,
    NarrativeSectionResponse,
    NarrativeSectionsListResponse,
    NarrativeSectionUpdateRequest,
    NarrativeSourcesResponse,
    NarrativeStatusResponse,
    PublicNarrativeSectionResponse,
    PublicNarrativeSectionsListResponse,
    RegenerateSectionResponseEnvelope,
    SourceMediaResponse,
    SourceMemoryResponse,
)

router = APIRouter(prefix="/api/memoirs", tags=["Narrative"])


def _resolve_user_id(current_user: dict) -> str:
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not resolve the authenticated user."
        )
    return str(user_id)


def _to_response(section: dict) -> NarrativeSectionResponse:
    return NarrativeSectionResponse(
        id=str(section["id"]),
        memoir_id=str(section["memoir_id"]),
        chapter_id=str(section["chapter_id"]) if section.get("chapter_id") else None,
        position=section["position"],
        body=section["body"],
        body_original=section["body_original"],
        owner_edited=bool(section.get("owner_edited")),
        source_memory_ids=[str(m) for m in section.get("source_memory_ids", [])],
        created_at=section["created_at"],
        updated_at=section["updated_at"],
    )


def _to_public_response(section: dict) -> PublicNarrativeSectionResponse:
    return PublicNarrativeSectionResponse(
        id=str(section["id"]),
        chapter_id=str(section["chapter_id"]) if section.get("chapter_id") else None,
        position=section["position"],
        body=section["body"],
        source_memory_ids=[str(m) for m in section.get("source_memory_ids", [])],
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@router.post(
    "/{memoir_id}/generate-narrative",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GenerateNarrativeResponseEnvelope,
)
async def generate_narrative(
    memoir_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    chapters = repo.fetch_chapters_for_memoir(memoir_id)
    if not chapters:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This memoir has no chapters yet. Run organization before generating the narrative.",
        )

    latest = repo.fetch_latest_run(memoir_id)
    if latest and narrative_service.compute_effective_status(latest) == "processing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Narrative generation is already in progress for this memoir.",
        )

    background_tasks.add_task(narrative_service.generate_narrative_background, memoir_id)

    return GenerateNarrativeResponseEnvelope()


@router.get(
    "/{memoir_id}/narrative/status",
    response_model=NarrativeStatusResponse,
)
async def get_narrative_status(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)

    run = repo.fetch_latest_run(memoir_id)
    if not run:
        return NarrativeStatusResponse(status="none")

    effective = narrative_service.compute_effective_status(run)
    warnings = run.get("warnings") or []
    return NarrativeStatusResponse(
        status=effective,
        error_message=run.get("error_message"),
        retry_available=effective in ("failed", "stalled"),
        warnings=[str(w) for w in warnings] if isinstance(warnings, list) else [],
    )


# ---------------------------------------------------------------------------
# Reading -- owner OR a reader with a valid unlocked share token
# ---------------------------------------------------------------------------


@router.get("/{memoir_id}/narrative")
async def list_narrative_sections(
    memoir_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    access = resolve_memoir_access(
        memoir_id=memoir_id,
        authorization=authorization,
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE_NAME),
    )
    sections = repo.fetch_narrative_sections(memoir_id)

    if access.kind == "owner":
        return NarrativeSectionsListResponse(data=[_to_response(s) for s in sections])
    return PublicNarrativeSectionsListResponse(data=[_to_public_response(s) for s in sections])


@router.get("/{memoir_id}/narrative/{section_id}/sources", response_model=NarrativeSourcesResponse)
async def get_narrative_sources(
    memoir_id: str,
    section_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    The Sources button: the exact memories a section cites, verbatim --
    text, photos, audio players, transcripts. Visible to the owner and to
    any reader who unlocked the share link, same access rule as reading the
    narrative itself.
    """
    resolve_memoir_access(
        memoir_id=memoir_id,
        authorization=authorization,
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE_NAME),
    )

    section = repo.fetch_narrative_section(section_id, memoir_id)
    memories = repo.fetch_source_memories(memoir_id, section["source_memory_ids"])

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


# ---------------------------------------------------------------------------
# Owner editing
# ---------------------------------------------------------------------------


@router.patch("/{memoir_id}/narrative/{section_id}", response_model=NarrativeSectionEnvelope)
async def update_narrative_section(
    memoir_id: str,
    section_id: str,
    payload: NarrativeSectionUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    updated = repo.update_section_body(section_id, memoir_id, payload.body)
    # Editing is the owner correcting/curating this exact text -- the prior
    # review no longer describes the current content.
    repo.clear_narrative_reviewed(memoir_id)

    return NarrativeSectionEnvelope(message="Section updated.", data=_to_response(updated))


@router.delete("/{memoir_id}/narrative/{section_id}", status_code=status.HTTP_200_OK)
async def delete_narrative_section(
    memoir_id: str,
    section_id: str,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    repo.delete_section(section_id, memoir_id)
    repo.clear_narrative_reviewed(memoir_id)

    return {"success": True, "message": "Section deleted."}


@router.post(
    "/{memoir_id}/narrative/{section_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RegenerateSectionResponseEnvelope,
)
async def regenerate_narrative_section(
    memoir_id: str,
    section_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    # Confirms the section exists (and belongs to this memoir) before
    # scheduling background work for it -- 404s immediately rather than
    # queuing a job that fails async for something checkable right now.
    repo.fetch_narrative_section(section_id, memoir_id)

    latest = repo.fetch_latest_run(memoir_id)
    if latest and narrative_service.compute_effective_status(latest) == "processing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A narrative operation is already in progress for this memoir.",
        )

    background_tasks.add_task(
        narrative_service.regenerate_section_background, memoir_id, section_id
    )
    return RegenerateSectionResponseEnvelope()


@router.post("/{memoir_id}/narrative/review", response_model=MarkReviewedResponseEnvelope)
async def mark_narrative_reviewed(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Required before publish whenever narrative sections exist. See
    MemoirService.publish_memoir's 409 for the enforcement side.
    """
    user_id = _resolve_user_id(current_user)
    narrative_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    reviewed_at = repo.mark_narrative_reviewed(memoir_id)
    return MarkReviewedResponseEnvelope(narrative_reviewed_at=reviewed_at)
