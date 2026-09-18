"""
@file domain/organization_service.py
@description Orchestrates the AI chapter-organization background job:
fetches finalized memories, asks the model to group them by ID only,
validates every returned ID against what was sent, and applies the result to
the database without ever leaving a partial write in place. Runs as a
FastAPI BackgroundTasks job (per direction, not Celery) so POST /organize
returns immediately while the ~10-20s model call happens out of band.
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import HTTPException, status
from openai import OpenAI

from src.core.config import settings
from src.integrations import organization_repository as repo
from src.integrations import participant_repository
from src.schemas.organization import ChatMessage, MemoirOrganizationOutput

logger = logging.getLogger(__name__)

OTHER_MEMORIES_TITLE = "Other Memories"
STALL_THRESHOLD_SECONDS = 5 * 60

# In-memory only -- resets on restart and isn't shared across worker
# processes. Good enough for a single-process BackgroundTasks deployment
# (same assumption the rest of this feature already makes); a real deployment
# with multiple workers would need a shared store (e.g. Redis) instead.
CHAT_RATE_LIMIT_MAX_MESSAGES = 10
CHAT_RATE_LIMIT_WINDOW_SECONDS = 60
_chat_request_log: Dict[str, List[float]] = defaultdict(list)

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return _client


SYSTEM_PROMPT = (
    "You are sorting a family's already-written memories into chronological "
    "chapters. You do not write, summarize, paraphrase, or invent anything -- "
    "you only group the memory IDs you are given and give each group a short "
    "title. Every memory_id you return must be one of the IDs you were given, "
    "copied exactly. Each memory_id may appear in at most one chapter. Assign "
    "a memory to a chapter only if you can place it confidently by date or "
    "content; if you are unsure where a memory belongs, omit it rather than "
    "guess -- an omitted memory is fine, a wrong guess is not. "
    "Return a JSON object matching exactly this shape, with no other fields:\n"
    '{"chapters": [{"title": "string", "sort_order": 1, '
    '"memories": [{"memory_id": "string", "inferred_date": "day|month|year|decade|null"}]}]}'
)


class OrganizationRejected(Exception):
    """Raised when the model's response can't be trusted and must be discarded whole (R4)."""


def verify_owner_access(memoir_id: str, user_id: str) -> None:
    """
    Organizing (and every manual edit on top of it) is owner-only (R1).
    Returns 404, not 403, so a caller who isn't a participant can't tell
    "not yours" from "doesn't exist" -- mirrors ExportService.verify_owner_access.
    """
    participant_res = participant_repository.fetch_participant(memoir_id, user_id)
    participants = participant_res.data or []
    if not participants or participants[0].get("role") != "owner":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")


def compute_effective_organization_status(job: dict) -> str:
    """
    Overrides a stuck 'running' with 'stalled' once it has run longer than
    STALL_THRESHOLD_SECONDS (R6). BackgroundTasks has no crash recovery or
    heartbeat, so a job that died with the server process would otherwise
    show 'running' forever -- same pattern as
    transcription_service.compute_effective_transcription_status.
    """
    current_status = job.get("organization_status")
    if current_status != "running":
        return current_status or "none"

    started_raw = job.get("organization_started_at")
    if not started_raw:
        return current_status

    try:
        started_at = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
    except ValueError:
        return current_status

    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return "stalled" if age_seconds > STALL_THRESHOLD_SECONDS else current_status


def _validate_llm_output(parsed: MemoirOrganizationOutput, sent_ids: set) -> None:
    """
    R4: an unknown memory ID means the model was confused, so the whole
    response is untrustworthy -- reject everything, not just the offending
    chapter. A memory duplicated across chapters is the same kind of
    confusion and is rejected the same way. Chapter count and title length
    are already bounded by the schema itself (MemoirOrganizationOutput /
    ChapterOutput) -- structured output guarantees shape, not content, so
    this function is what actually checks content.
    """
    seen_ids = set()
    for chapter in parsed.chapters:
        for mapping in chapter.memories:
            if mapping.memory_id not in sent_ids:
                raise OrganizationRejected(f"Model returned unknown memory_id={mapping.memory_id!r}.")
            if mapping.memory_id in seen_ids:
                raise OrganizationRejected(f"Model placed memory_id={mapping.memory_id!r} in more than one chapter.")
            seen_ids.add(mapping.memory_id)


def perform_background_organization(memoir_id: str) -> None:
    """
    Background worker. Always resolves organization_status to 'ready' or
    'failed' -- nothing upstream is waiting on a return value, so every exit
    path here must record its own outcome.
    """
    try:
        memories = repo.fetch_memories_for_ai(memoir_id)
        if not memories:
            repo.set_organization_job_status(
                memoir_id, "failed", error_message="There are no saved memories yet to organize."
            )
            return

        sent_ids = {str(m["id"]) for m in memories}
        payload_for_llm = [
            {
                "memory_id": str(m["id"]),
                "title": m.get("title") or "",
                "text": m.get("body_text") or "",
                "recorded_date": str(m.get("occurred_start") or ""),
            }
            for m in memories
        ]

        response = _get_client().chat.completions.create(
            model="gemini-3.6-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload_for_llm)},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content

        try:
            validated = MemoirOrganizationOutput.model_validate_json(content)
        except Exception as parse_err:
            raise OrganizationRejected(f"Model response failed schema validation: {parse_err}") from parse_err

        _validate_llm_output(validated, sent_ids)

        returned_ids = {m.memory_id for c in validated.chapters for m in c.memories}
        forgotten_ids = sorted(sent_ids - returned_ids)  # R5: never silently dropped

        _apply_validated_organization(memoir_id, validated, forgotten_ids)

        repo.set_organization_job_status(memoir_id, "ready")
        logger.info("AI organization applied for memoir_id=%s (%d forgotten memories)", memoir_id, len(forgotten_ids))

    except OrganizationRejected as rejected:
        logger.warning("AI organization rejected for memoir_id=%s: %s", memoir_id, rejected)
        repo.set_organization_job_status(
            memoir_id,
            "failed",
            error_message="The AI's grouping couldn't be trusted, so nothing was changed. You can try again.",
        )
    except Exception:
        logger.exception("AI organization failed for memoir_id=%s", memoir_id)
        repo.set_organization_job_status(
            memoir_id,
            "failed",
            error_message="Something went wrong while organizing this memoir. You can try again.",
        )


def _apply_validated_organization(memoir_id: str, validated: MemoirOrganizationOutput, forgotten_ids: List[str]) -> None:
    """
    Applies an already-validated result without ever leaving a partial state
    visible (R7): new chapters are built and fully populated first, and the
    previous AI-authored chapters are only removed once the new structure is
    completely in place. Any failure along the way restores every touched
    memory to its prior chapter/date-precision and deletes the newly created
    chapters, so the old organization is left exactly as it was.

    This isn't a real database transaction (the Supabase REST client doesn't
    give us one) -- it's a best-effort compensating rollback. Documented as a
    known limitation in the integration report rather than silently assumed
    to be atomic.
    """
    all_touched_ids = list({m.memory_id for c in validated.chapters for m in c.memories} | set(forgotten_ids))
    before_snapshot = repo.fetch_memory_chapter_snapshot(all_touched_ids, memoir_id)

    new_chapter_ids: List[str] = []
    try:
        for chapter in validated.chapters:
            new_chapter_ids.append(repo.insert_chapter(memoir_id, chapter.title, chapter.sort_order))
        if forgotten_ids:
            other_sort_order = max((c.sort_order for c in validated.chapters), default=0) + 1
            new_chapter_ids.append(repo.insert_chapter(memoir_id, OTHER_MEMORIES_TITLE, other_sort_order))
    except Exception:
        repo.delete_chapters(new_chapter_ids)
        raise

    try:
        for chapter, chapter_id in zip(validated.chapters, new_chapter_ids):
            for mapping in chapter.memories:
                repo.set_memory_chapter(mapping.memory_id, memoir_id, chapter_id, mapping.inferred_date)

        if forgotten_ids:
            other_chapter_id = new_chapter_ids[-1]
            for memory_id in forgotten_ids:
                repo.set_memory_chapter(memory_id, memoir_id, other_chapter_id)
    except Exception:
        for memory_id, before in before_snapshot.items():
            repo.set_memory_chapter(memory_id, memoir_id, before.get("chapter_id"), before.get("occurred_precision"))
        repo.delete_chapters(new_chapter_ids)
        raise

    stale_chapter_ids = [cid for cid in repo.fetch_existing_chapter_ids(memoir_id) if cid not in new_chapter_ids]
    repo.delete_chapters(stale_chapter_ids)


CHAT_SYSTEM_PROMPT = (
    "You are an empathetic, insightful archival co-author assisting a user with their "
    "family memoir. You have access to the structured table of contents of their "
    "archive below -- chapter titles, summaries, and memory titles/dates, not the full "
    "memory text. Use this to answer questions, suggest chapter improvements, or help "
    "brainstorm ideas. Keep your tone warm, encouraging, and focused on storytelling. "
    "You are a conversational assistant only: you never rewrite, summarize, or merge "
    "the family's actual memories -- that stays entirely in their own words.\n\n"
)


def _check_chat_rate_limit(rate_limit_key: str) -> None:
    """
    Caps chat volume per (user, memoir) pair. Chat has no requirement written
    for it, but every message re-sends the archive context to Gemini on the
    caller's behalf with no cap otherwise -- an unbounded loop (buggy client
    or otherwise) would run up API spend with nothing to stop it.
    """
    now = time.monotonic()
    window_start = now - CHAT_RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _chat_request_log[rate_limit_key] if t > window_start]
    if len(recent) >= CHAT_RATE_LIMIT_MAX_MESSAGES:
        _chat_request_log[rate_limit_key] = recent
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="You're sending messages too quickly. Please wait a moment and try again.",
        )
    recent.append(now)
    _chat_request_log[rate_limit_key] = recent


def _build_archive_context(memoir_id: str) -> str:
    """Text-only table of contents (chapter titles/summaries, memory titles/dates) -- no memory body text (R8's spirit)."""
    raw = repo.fetch_archive_index(memoir_id)
    chapters = raw["chapters"]
    memories = raw["memories"]

    lines = ["== MEMOIR ARCHIVE TABLE OF CONTENTS =="]
    for chapter in chapters:
        lines.append(f"\nChapter {chapter.get('sort_order')}: {chapter.get('title')}")
        if chapter.get("summary"):
            lines.append(f"Summary: {chapter['summary']}")
        chapter_memories = [m for m in memories if m.get("chapter_id") == chapter.get("id")]
        if chapter_memories:
            lines.append("Contained memories:")
            for memory in chapter_memories:
                lines.append(f"  - [{memory.get('occurred_start') or 'Undated'}] {memory.get('title') or 'Untitled'}")
        else:
            lines.append("  (no memories assigned yet)")

    unassigned = [m for m in memories if not m.get("chapter_id")]
    if unassigned:
        lines.append("\nUnassigned memories:")
        for memory in unassigned:
            lines.append(f"  - {memory.get('title') or 'Untitled'}")

    return "\n".join(lines)


def chat_with_archive(memoir_id: str, user_id: str, message: str, history: List[ChatMessage]) -> str:
    """
    Conversational Q&A over a memoir's chapter/memory structure. Read-only --
    never writes to chapters or memories, so unlike /organize it can't
    corrupt data; the risk here is cost and exposure of memory titles to
    Gemini, which the per-user rate limit and title-only context bound.
    """
    _check_chat_rate_limit(f"{user_id}:{memoir_id}")

    archive_context = _build_archive_context(memoir_id)
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT + archive_context}]
    for turn in history:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": message})

    try:
        response = _get_client().chat.completions.create(
            model="gemini-3.6-flash",
            messages=messages,
        )
        return response.choices[0].message.content
    except Exception as exc:
        error_str = str(exc)
        if "429" in error_str or "ResourceExhausted" in error_str or "Too Many Requests" in error_str:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="The AI service is busy right now. Please wait a moment and try again.",
            )
        logger.exception("Archive chat failed for memoir_id=%s", memoir_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The AI co-author couldn't respond right now. Please try again.",
        )
