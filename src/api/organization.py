"""
@file api/organization.py
@description Routes for AI chapter organization: trigger, status polling,
owner-driven manual chapter/memory edits, and the archive chat.

Every route re-verifies access server-side from the authenticated user. A
`memoir_id` in the path is a hint, never authorization — the reference
implementation accepted `current_user` on all three of its chapter routes and
never referenced it, leaving the entire feature open to any logged-in user.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from src.core.auth import get_current_user
from src.domain import organization_service
from src.domain.authorization import assert_memoir_editable, verify_active_participant
from src.domain.organization_service import perform_background_organization
from src.integrations import organization_repository as repo
from src.integrations import organization_run_repository as run_repo
from src.integrations import organization_view_repository as view_repo
from src.integrations import proposal_repository as proposal_repo
from src.schemas.organization import (
    ActionHistoryEntry,
    ActionHistoryResponse,
    ApplyProposalRequest,
    ChapterReorderRequest,
    ChapterUpdateRequest,
    ChatRequest,
    ChatResponse,
    MemoryMoveRequest,
    OrganizeResponseEnvelope,
    OrganizeStatusResponse,
    ProposalListResponse,
    ProposalReviewResponse,
    ResolvedPlan,
)

organization_router = APIRouter(prefix="/api/memoirs", tags=["AI Organization"])

logger = logging.getLogger(__name__)


def _resolve_user_id(current_user: dict) -> str:
    """
    Extracts the user id from the auth dependency's dict.

    `get_current_user` returns a dict, not a string. Several endpoints annotate
    it as `str` and rely on a defensive unwrap deep in authorization.py — that
    works by accident and breaks confusingly if the unwrap is ever cleaned up.
    Resolving it explicitly once, here, is the fix.
    """
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not resolve the authenticated user."
        )
    return str(user_id)


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
    """
    Owner-only, blocked on a published memoir.

    Runs the pipeline in the background and produces a *proposal*. Nothing is
    written to chapters until the owner confirms via POST /organize/apply — see
    confirm_proposal for why that separation is structural rather than
    cosmetic.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    existing_job = repo.fetch_organization_job_status(memoir_id)
    if (
        existing_job
        and organization_service.compute_effective_organization_status(existing_job) == "running"
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization is already in progress for this memoir.",
        )

    # Status is set before the background task is scheduled, not inside it, so
    # a caller who polls immediately cannot observe scheduled work as 'none'.
    repo.set_organization_job_status(memoir_id, "running", started=True)

    # Sync callable, deliberately. See the note in organization_service: an
    # async background task is awaited on the event loop, and this pipeline runs
    # for tens of seconds to minutes.
    background_tasks.add_task(perform_background_organization, memoir_id)

    return OrganizeResponseEnvelope(
        success=True, message="Organization started in the background.", status="processing"
    )


@organization_router.get(
    "/{memoir_id}/organize/proposal",
    response_model=ProposalListResponse,
)
async def get_pending_proposal(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Returns the current pending proposal for owner review.

    404 rather than an empty body when there is none: the caller needs to
    distinguish "nothing to review" from "not yours", and only the second is a
    404.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)

    proposal = proposal_repo.fetch_pending_proposal(memoir_id)
    if not proposal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No pending proposal to review."
        )

    plan = ResolvedPlan.model_validate(proposal["payload"])
    placed = len(plan.all_assigned_ids())

    # The owner is about to be asked to confirm a destructive change — this
    # proposal replaces whatever AI chapters exist. Saying "this will replace 0
    # chapters" when it replaces four is not a cosmetic gap: it is the difference
    # between a review and a click. This was left at its default of 0 before,
    # which is exactly the "looks good" screen the spec forbids.
    chapters_replaced = len(repo.fetch_existing_chapter_ids(memoir_id))

    return ProposalListResponse(
        data=ProposalReviewResponse(
            proposal_id=proposal["id"],
            status=proposal["status"],
            source=proposal.get("source") or "organize",
            summary_line=proposal.get("summary_line") or "",
            plan=plan,
            warnings=organization_service.build_proposal_warnings(
                plan,
                # The plan never contains owner-locked memories, so their count
                # is not recoverable from the stored payload. Recomputing it here
                # keeps the review screen identical to the one shown at creation
                # time, which is what makes the second read a review rather than
                # a rubber stamp.
                locked_count=len(repo.fetch_owner_locked_memory_ids(memoir_id)),
                chapters_replaced=chapters_replaced,
            ),
            memories_considered=placed + len(plan.unplaced_memory_ids),
            memories_placed=placed,
            memories_unplaced=len(plan.unplaced_memory_ids),
            chapters_replaced=chapters_replaced,
        )
    )


@organization_router.post(
    "/{memoir_id}/organize/apply",
    status_code=status.HTTP_200_OK,
)
async def apply_organization_proposal(
    memoir_id: str,
    payload: ApplyProposalRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Applies a reviewed proposal. Owner-only, blocked on a published memoir.

    The request body carries an id and nothing else. All re-validation
    (membership, status, expiry, editability) happens server-side at
    confirmation time.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    # confirm_proposal already records the 'apply_proposal' audit entry with the
    # acting user. Not duplicated here.
    plan = await organization_service.confirm_proposal(payload.proposal_id, memoir_id, user_id)

    return {
        "success": True,
        "message": (
            f"Applied {len(plan.chapters)} chapters. Your original memories were not changed."
        ),
        "data": {
            "chapter_count": len(plan.chapters),
            "memories_placed": len(plan.all_assigned_ids()),
            "memories_unplaced": len(plan.unplaced_memory_ids),
        },
    }


@organization_router.get(
    "/{memoir_id}/organize/memories",
    status_code=status.HTTP_200_OK,
)
async def get_memories_for_organization(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Owner-only. Every submitted memory with its current chapter assignment.

    Needed because `GET /chapters` cannot serve the review screen: it joins
    chapters to the memories already assigned to them, so every memory the AI has
    not placed is invisible -- which is precisely the set the owner most needs to
    see before approving a grouping.

    Returns `body_text` deliberately. The owner wrote it and is being asked to
    confirm how it is grouped; a title-only list would make the review
    impossible to perform honestly. It is never returned to a non-owner, because
    `verify_owner_access` runs first and the query is `memoir_id`-scoped.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)

    memories = view_repo.fetch_submitted_memories_for_review(memoir_id)
    counts = view_repo.fetch_memory_counts_for_review(memoir_id)

    return {
        "success": True,
        "data": memories,
        "counts": counts,
    }


@organization_router.get(
    "/{memoir_id}/organize/history",
    response_model=ActionHistoryResponse,
)
async def get_organization_history(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only audit trail of changes made to this memoir's chapters."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)

    entries = proposal_repo.fetch_action_history(memoir_id)
    return ActionHistoryResponse(
        data=[ActionHistoryEntry(**entry) for entry in entries]
    )


@organization_router.get(
    "/{memoir_id}/organize/status",
    response_model=OrganizeStatusResponse,
)
async def get_organization_status(
    memoir_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Owner-only. A 'running' job past the stall threshold reports 'stalled'.

    Stage progress is read from `organization_agent_run`, not invented. The
    previous version returned a hardcoded four-item list with
    `current_stage` pinned to element zero, so the UI showed "reading memories"
    from the first second of a two-minute run and kept showing it while the
    organizer was already writing summaries. A progress indicator that does not
    move is worse than none: it tells the owner to wait when they should be
    deciding whether to give up.

    Falls back to the memoir-level status when no run row exists — runs created
    before this table did, or on a memoir whose run row failed to open.
    """
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)

    job = repo.fetch_organization_job_status(memoir_id)
    run = None
    try:
        run = run_repo.fetch_latest_run(memoir_id)
    except Exception:
        # Progress is a convenience. A failure to read the run table must not
        # turn a status poll into a 500 — the owner still needs to know whether
        # their organize job is running.
        logger.exception("could not read organization run rows (memoir_id=%s)", memoir_id)

    if not job or not job.get("organization_status"):
        return OrganizeStatusResponse(status="none")

    effective_status = organization_service.compute_effective_organization_status(job)
    in_progress = effective_status in ("running", "queued")

    agent_runs = (run or {}).get("agent_runs") or []
    stages = [
        {
            "name": stage.get("agent_role"),
            "status": stage.get("status"),
        }
        for stage in agent_runs
    ]

    current_stage = next(
        (s.get("agent_role") for s in agent_runs if s.get("status") == run_repo.RUN_STATUS_RUNNING),
        None,
    )

    return OrganizeStatusResponse(
        status=effective_status,
        error_message=job.get("organization_error_message"),
        retry_available=effective_status in ("failed", "stalled"),
        current_stage=current_stage,
        stages=stages if in_progress else [],
        provider_used=(run or {}).get("provider_used"),
    )


@organization_router.put("/{memoir_id}/chapters/reorder", status_code=status.HTTP_200_OK)
async def manual_reorder_chapters(
    memoir_id: str,
    payload: ChapterReorderRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Owner-only, blocked on a published memoir.

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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more chapters do not belong to this memoir.",
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
    """Owner-only, blocked on a published memoir. Locks the chapter from AI edits."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    if payload.title is None and payload.summary is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Must provide title or summary to update."
        )

    updated_chapter = repo.update_chapter_in_db(
        chapter_id, memoir_id, title=payload.title, summary=payload.summary
    )
    return {
        "success": True,
        "message": "Chapter updated and locked against future AI changes.",
        "data": updated_chapter,
    }


@organization_router.put("/{memoir_id}/memories/{memory_id}/move", status_code=status.HTTP_200_OK)
async def manual_move_memory(
    memoir_id: str,
    memory_id: str,
    payload: MemoryMoveRequest,
    current_user: dict = Depends(get_current_user),
):
    """Owner-only, blocked on a published memoir."""
    user_id = _resolve_user_id(current_user)
    organization_service.verify_owner_access(memoir_id, user_id)
    assert_memoir_editable(memoir_id)

    updated_memory = repo.move_memory_in_db(memoir_id, memory_id, payload.new_chapter_id)
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
    Any active participant may converse with the AI co-author about the archive's
    structure. Read-only: never writes to chapters or memories.

    Returns 403 rather than 404 on access denial here, matching this file's
    participant-level read (get_memoir_chapters).
    """
    user_id = _resolve_user_id(current_user)
    verify_active_participant(memoir_id, user_id)

    reply, proposed_actions = await organization_service.chat_with_archive(
        memoir_id, user_id, payload.message, payload.history
    )
    return ChatResponse(success=True, reply=reply, proposed_actions=proposed_actions)


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