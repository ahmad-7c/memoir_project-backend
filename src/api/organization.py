"""
@file api/organization.py
@description Routes for AI chapter organization: trigger, status polling, and
owner-driven manual chapter/memory edits.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from src.core.auth import get_current_user
from src.domain import organization_service
from src.domain.authorization import assert_memoir_editable, verify_active_participant
from src.domain.organization_service import perform_background_organization
from src.integrations import organization_repository as repo
from src.schemas.organization import (
    ChapterReorderRequest,
    ChapterUpdateRequest,
    ChatRequest,
    ChatResponse,
    MemoryMoveRequest,
    OrganizeResponseEnvelope,
    OrganizeStatusResponse,
)

organization_router = APIRouter(prefix="/api/memoirs", tags=["AI Organization"])


def _resolve_user_id(current_user: dict) -> str:
    return str(current_user.get("user_id") or current_user.get("id") or current_user.get("sub"))


@organization_router.post(
    "/{memoir_id}/organize",
    response_model=OrganizeResponseEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_ai_organization(
    memoir_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only (R1), blocked on a published memoir (R2)."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    existing_job = repo.fetch_organization_job_status(memoir_id)
    if existing_job and organization_service.compute_effective_organization_status(existing_job) == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization is already in progress for this memoir.",
        )

    repo.set_organization_job_status(memoir_id, "running", started=True)
    background_tasks.add_task(perform_background_organization, memoir_id)

    return OrganizeResponseEnvelope(success=True, message="Organization started in the background.", status="processing")


@organization_router.get(
    "/{memoir_id}/organize/status",
    response_model=OrganizeStatusResponse,
)
async def get_organization_status(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only. A 'running' job stuck past 5 minutes is reported as 'stalled' with a retry available (R6)."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)

    job = repo.fetch_organization_job_status(memoir_id)
    if not job or not job.get("organization_status"):
        return OrganizeStatusResponse(status="none")

    effective_status = organization_service.compute_effective_organization_status(job)
    return OrganizeStatusResponse(
        status=effective_status,
        error_message=job.get("organization_error_message"),
        retry_available=effective_status in ("failed", "stalled"),
    )


@organization_router.put("/{memoir_id}/chapters/reorder", status_code=status.HTTP_200_OK)
async def manual_reorder_chapters(
    memoir_id: str,
    payload: ChapterReorderRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Owner-only (R1), blocked on a published memoir (R2). Added to close the
    R10 gap -- the reference branch had no way to reorder chapters at all.

    Registered before /{memoir_id}/chapters/{chapter_id} so the literal
    "reorder" path isn't swallowed by the parameterized route.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    existing_ids = set(repo.fetch_chapter_ids_for_memoir(memoir_id))
    requested_ids = {entry.chapter_id for entry in payload.order}
    if not requested_ids.issubset(existing_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="One or more chapters do not belong to this memoir."
        )

    updated = repo.reorder_chapters_in_db(memoir_id, [entry.model_dump() for entry in payload.order])
    return {"success": True, "message": "Chapter order updated.", "data": updated}


@organization_router.put("/{memoir_id}/chapters/{chapter_id}", status_code=status.HTTP_200_OK)
async def manual_update_chapter(
    memoir_id: str,
    chapter_id: str,
    payload: ChapterUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only (R1), blocked on a published memoir (R2 -- missing entirely in the reference branch)."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    if payload.title is None and payload.summary is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Must provide title or summary to update.")

    updated_chapter = repo.update_chapter_in_db(chapter_id, memoir_id, title=payload.title, summary=payload.summary)
    return {"success": True, "message": "Chapter updated and locked against future AI changes.", "data": updated_chapter}


@organization_router.put("/{memoir_id}/memories/{memory_id}/move", status_code=status.HTTP_200_OK)
async def manual_move_memory(
    memoir_id: str,
    memory_id: str,
    payload: MemoryMoveRequest,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only (R1), blocked on a published memoir (R2 -- missing entirely in the reference branch)."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    updated_memory = repo.move_memory_in_db(memory_id, memoir_id, payload.new_chapter_id)
    return {"success": True, "message": "Memory moved.", "data": updated_memory}


@organization_router.post(
    "/{memoir_id}/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
)
async def chat_with_archive(
    memoir_id: str,
    payload: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Any active participant may converse with the AI co-author about the
    archive's structure (chapter/memory titles and dates only -- see
    organization_service._build_archive_context). Read-only: never writes to
    chapters or memories. No written requirement covers this feature; kept
    to a 403 (not 404) on access denial to match this file's other
    participant-level read (get_memoir_chapters), and rate-limited per
    (user, memoir) since nothing else here bounds Gemini API spend.
    """
    user_id = _resolve_user_id(current_user)
    verify_active_participant(memoir_id, user_id)

    reply = organization_service.chat_with_archive(memoir_id, user_id, payload.message, payload.history)
    return ChatResponse(success=True, reply=reply)


@organization_router.get("/{memoir_id}/chapters", status_code=status.HTTP_200_OK)
async def get_memoir_chapters(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Any active participant may view the chapter structure."""
    user_id = _resolve_user_id(current_user)
    verify_active_participant(memoir_id, user_id)

    chapters = repo.fetch_chapters_with_memories(memoir_id)
    return {"success": True, "message": "Chapters fetched successfully.", "data": chapters}
