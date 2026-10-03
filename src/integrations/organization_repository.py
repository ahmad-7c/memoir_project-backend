"""
@file integrations/organization_repository.py
@description Data access for AI chapter organization: fetching memories and
chapters, persisting a resolved plan, owner-driven manual edits, and
organize-job status tracking.

Every mutation here takes `memoir_id` and scopes by it. That is not defensive
style — it is the only thing preventing cross-tenant writes, because
`supabase_admin` bypasses Postgres RLS entirely. The reference implementation's
`apply_chapters_to_db` updated memories with `.eq("id", ...)` and no memoir
scope, driven by an unvalidated request body: any logged-in user could re-point
another family's memories into their own memoir.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import HTTPException, status

from src.integrations.supabase_client import supabase_admin

# PostgREST caps the number of values in an `in_()` filter. Chunking below this
# keeps large memoirs from silently failing to filter.
_IN_FILTER_CHUNK = 100

# Rows per multi-row upsert. Supabase's REST layer handles a few hundred rows
# comfortably; larger payloads get rejected rather than truncated.
_UPSERT_CHUNK = 200


def fetch_memories_for_ai(memoir_id: str) -> List[Dict[str, Any]]:
    """
    Fetches finalized memories for the memoir.

    Only 'submitted' memories are organized: 'draft' is the default state a
    memory sits in while its author is still writing it. Organizing drafts would
    group text the author hasn't chosen to submit. The reference implementation
    omitted this filter and organized drafts.
    """
    res = (
        supabase_admin.table("memory")
        .select("id, title, body_text, occurred_start")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return res.data or []


def fetch_submitted_memory_ids(memoir_id: str) -> List[str]:
    """
    Ids only — no body_text, no titles.

    Confirmation-time validation needs to answer "is every id in this plan still a
    member of this memoir's submitted set?", and that question is about identity
    alone. Fetching full bodies to answer it means pulling every word a family
    wrote, over the network, into a process that will only ever call `.id` on it
    — on a 500-memory memoir that is megabytes per confirmation attempt.

    Kept as a separate function rather than a flag on `fetch_memories_for_ai` so
    that the cheap path stays cheap: a flag would be a per-row branch and an
    accidental truthy value would pull the whole memoir back.
    """
    res = (
        supabase_admin.table("memory")
        .select("id")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return [row["id"] for row in (res.data or [])]


def fetch_owner_locked_memory_ids(memoir_id: str) -> Set[str]:
    """
    Memories currently sitting in a chapter the owner has edited by hand.

    These are held out of every AI proposal entirely. An earlier version let the
    organizer see them, which produced a quietly destructive result: the owner
    edits "The Boat Years", locks it, then reorganizes — and the new AI chapters
    claim those memories, leaving their chapter with an empty heading that still
    renders in the reader-facing book. The owner did everything the UI promised
    (their chapter is marked locked) and still lost the contents.

    Holding them out is the honest reading of `edited_by_owner`: not "don't
    overwrite this chapter's title and prose" but "this grouping is the owner's
    now". The AI organizes what is left, and the review screen says so.

    Two queries rather than a PostgREST embedding join: embedding is not used
    anywhere in this codebase, and introducing it for one call would add a new
    failure mode to a path that has to work.
    """
    owner_edited_chapter_ids = fetch_existing_chapter_ids_by_owner_edit(memoir_id)
    if not owner_edited_chapter_ids:
        return set()

    locked: Set[str] = set()
    for chunk in _chunked(owner_edited_chapter_ids, _IN_FILTER_CHUNK):
        rows = (
            supabase_admin.table("memory")
            .select("id")
            .eq("memoir_id", memoir_id)
            .in_("chapter_id", chunk)
            .execute()
        )
        locked.update(row["id"] for row in (rows.data or []))
    return locked


def fetch_existing_chapter_ids_by_owner_edit(memoir_id: str) -> List[str]:
    """Chapters the owner has edited by hand, which AI must not reorganize."""
    res = (
        supabase_admin.table("chapter")
        .select("id")
        .eq("memoir_id", memoir_id)
        .eq("edited_by_owner", True)
        .execute()
    )
    return [row["id"] for row in (res.data or [])]


def fetch_existing_chapter_ids(memoir_id: str) -> List[str]:
    """AI-authored, not-yet-owner-edited chapters -- the only ones a run may replace."""
    res = (
        supabase_admin.table("chapter")
        .select("id")
        .eq("memoir_id", memoir_id)
        .eq("edited_by_owner", False)
        .execute()
    )
    return [row["id"] for row in (res.data or [])]


def fetch_existing_chapter_ids_and_titles(memoir_id: str) -> List[Dict[str, Any]]:
    """
    Chapter id/title/summary for continuity of naming between runs.

    Named columns only. The reference implementation used `select("*")` here,
    which pulls internal columns into memory for no benefit.
    """
    res = (
        supabase_admin.table("chapter")
        .select("id, title, summary, sort_order")
        .eq("memoir_id", memoir_id)
        .execute()
    )
    return res.data or []


def insert_chapters(
    memoir_id: str, chapters: List[Dict[str, Any]]
) -> List[str]:
    """
    Inserts AI-authored chapters in one round-trip and returns their ids in order.

    Three deliberate choices here.

    1. `edited_by_owner` is always False and `created_by` always "ai". The
       reference implementation wrote True, which is a false provenance claim
       about AI output and, worse, makes every future organize run treat its own
       output as untouchable.

    2. Ids are generated here rather than left to the database. A multi-row
       PostgREST insert returns its rows in insertion order, which is documented
       behaviour and which the caller depends on to zip ids back onto plans —
       but it is not something to be casual about, and generating the ids makes
       the correspondence a fact of the request rather than a property of the
       response. It also gives the rollback path the exact ids to delete without
       a re-read.

    3. One call, not one per chapter. This is bounded (at most 13 after the
       resolver) so it was never a cliff, but the previous per-chapter loop was
       already a needless round-trip inside a background job.
    """
    if not chapters:
        return []

    rows = []
    ids: List[str] = []
    for chapter in chapters:
        chapter_id = str(uuid.uuid4())
        ids.append(chapter_id)
        rows.append(
            {
                "id": chapter_id,
                "memoir_id": memoir_id,
                "title": chapter["title"],
                "summary": chapter.get("summary") or "",
                "sort_order": chapter.get("sort_order", 0),
                "created_by": "ai",
                "edited_by_owner": False,
            }
        )

    for chunk in _chunked(rows, _UPSERT_CHUNK):
        res = supabase_admin.table("chapter").insert(chunk).execute()
        if not res.data or len(res.data) != len(chunk):
            raise RuntimeError("Failed to insert chapter rows.")

    return ids


def delete_chapters(memoir_id: str, chapter_ids: List[str]) -> None:
    """
    Deletes chapters, scoped to the memoir.

    The `memoir_id` filter is mandatory, not defensive. This is a rollback path
    reached from exception handling, which is exactly where a bug is least
    likely to be noticed and an unscoped delete most damaging.
    """
    if not chapter_ids:
        return
    for chunk in _chunked(list(chapter_ids), _IN_FILTER_CHUNK):
        supabase_admin.table("chapter").delete().eq("memoir_id", memoir_id).in_("id", chunk).execute()


def set_memory_chapter(
    memory_id: str,
    memoir_id: str,
    chapter_id: Optional[str],
    occurred_precision: Optional[str] = None,
    position_in_chapter: Optional[int] = None,
) -> None:
    update_payload: Dict[str, Any] = {"chapter_id": chapter_id}
    if occurred_precision is not None:
        update_payload["occurred_precision"] = occurred_precision
    if position_in_chapter is not None:
        update_payload["position_in_chapter"] = position_in_chapter
    supabase_admin.table("memory").update(update_payload).eq("id", memory_id).eq("memoir_id", memoir_id).execute()


def assign_memories_to_chapters(
    memoir_id: str,
    assignments: List[Dict[str, Any]],
    verified_ids: Optional[Iterable[str]] = None,
) -> None:
    """
    Writes chapter_id + position for many memories.

    Batched rather than a per-memory loop: the previous implementation issued one
    sequential HTTP round-trip per memory inside a background job, so a
    500-memory memoir meant 500 sequential calls — a scalability cliff that only
    appears under real data volume.

    `upsert` is used rather than `update` because PostgREST cannot express
    per-row values — each memory gets a different `chapter_id` and a different
    `position_in_chapter`, and there is no CASE expression available short of an
    RPC we do not have.

    That makes `upsert` the one write primitive here with a real hazard: it
    inserts when the row is absent. An insert here would try to create a memory
    from only four columns and fail on the NOT NULL constraints, so the failure
    mode is loud rather than a phantom memory — but loud is not the same as
    prevented, and it fails at the database rather than at the check that should
    have caught it. `verified_ids` is therefore required by the caller and
    asserted here. An unverified id means the caller skipped membership
    re-validation, which is the check that makes this write safe at all.

    Still chunked, because an unbounded multi-row write is rejected outright by
    PostgREST.
    """
    if not assignments:
        return

    if verified_ids is None:
        raise ValueError(
            "assign_memories_to_chapters requires verified_ids: the caller must "
            "have re-checked membership at write time."
        )

    verified = set(verified_ids)
    unverified = sorted(
        {e["memory_id"] for e in assignments if e["memory_id"] not in verified}
    )
    if unverified:
        raise ValueError(
            f"{len(unverified)} memory id(s) in the plan are not members of this "
            "memoir's submitted set; refusing to write."
        )

    for chunk in _chunked(assignments, _UPSERT_CHUNK):
        supabase_admin.table("memory").upsert(
            [
                {
                    "id": entry["memory_id"],
                    "memoir_id": memoir_id,
                    "chapter_id": entry["chapter_id"],
                    "position_in_chapter": entry.get("position"),
                }
                for entry in chunk
            ],
            on_conflict="id",
        ).execute()


def fetch_memory_chapter_snapshot(memory_ids: List[str], memoir_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Captures each memory's chapter_id/occurred_precision before a run touches it,
    so a failed run can restore exactly what was there.
    """
    if not memory_ids:
        return {}

    snapshot: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunked(list(memory_ids), _IN_FILTER_CHUNK):
        res = (
            supabase_admin.table("memory")
            .select("id, chapter_id, occurred_precision, position_in_chapter")
            .in_("id", chunk)
            .eq("memoir_id", memoir_id)
            .execute()
        )
        for row in res.data or []:
            snapshot[row["id"]] = row

    return snapshot


def update_chapter_in_db(
    chapter_id: str, memoir_id: str, title: Optional[str] = None, summary: Optional[str] = None
) -> dict:
    """
    Updates a chapter's owner-facing text and locks it from future AI overwrites.

    `edited_by_owner` is set unconditionally: any owner edit makes the chapter
    permanently off-limits to the organizer, which is the behaviour the UI
    promises when it says "locked against future AI changes".
    """
    update_payload: Dict[str, Any] = {"edited_by_owner": True}
    if title is not None:
        update_payload["title"] = title
    if summary is not None:
        update_payload["summary"] = summary

    res = (
        supabase_admin.table("chapter")
        .update(update_payload)
        .eq("id", chapter_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chapter not found or does not belong to this memoir.",
        )
    return res.data[0]


def fetch_chapter_ids_for_memoir(memoir_id: str) -> List[str]:
    res = supabase_admin.table("chapter").select("id").eq("memoir_id", memoir_id).execute()
    return [row["id"] for row in (res.data or [])]


def reorder_chapters_in_db(memoir_id: str, order: List[Dict[str, Any]]) -> List[dict]:
    """
    Applies a new sort_order to each listed chapter.

    Caller must already have verified every chapter_id belongs to this memoir
    AND that the list has no duplicates (the request schema enforces the latter).

    Batched per distinct sort_order value rather than per chapter: a reorder
    usually assigns the same order to one chapter, so this collapses N writes
    into a handful. Still per-value rather than one big CASE statement, because
    that would need an RPC we don't have.
    """
    by_order: Dict[int, List[str]] = {}
    for entry in order:
        by_order.setdefault(entry["sort_order"], []).append(entry["chapter_id"])

    for sort_order, chapter_ids in by_order.items():
        for chunk in _chunked(chapter_ids, _IN_FILTER_CHUNK):
            supabase_admin.table("chapter").update({"sort_order": sort_order}).eq(
                "memoir_id", memoir_id
            ).in_("id", chunk).execute()

    return [{"id": entry["chapter_id"], "sort_order": entry["sort_order"]} for entry in order]


def move_memory_in_db(memoir_id: str, memory_id: str, new_chapter_id: str) -> dict:
    """Relocates a memory, verifying the target chapter belongs to this memoir."""
    chapter_check = (
        supabase_admin.table("chapter")
        .select("id")
        .eq("id", new_chapter_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not chapter_check.data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid target chapter: it does not belong to this memoir.",
        )

    res = (
        supabase_admin.table("memory")
        .update({"chapter_id": new_chapter_id})
        .eq("id", memory_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found or does not belong to this memoir.",
        )
    return res.data[0]


def fetch_chapters_with_memories(memoir_id: str) -> List[dict]:
    """
    Chapters with their assigned memories, in sort order.

    Two queries and an in-memory join, not a per-chapter query: the previous
    implementation already did this correctly and it stays.
    """
    chapters_res = (
        supabase_admin.table("chapter")
        .select("id, title, summary, sort_order, created_by, edited_by_owner")
        .eq("memoir_id", memoir_id)
        .order("sort_order")
        .execute()
    )
    chapters = chapters_res.data or []

    memories_res = (
        supabase_admin.table("memory")
        .select("id, title, body_text, occurred_start, occurred_precision, chapter_id, position_in_chapter")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    memories = memories_res.data or []

    by_chapter: Dict[str, List[dict]] = {}
    for memory in memories:
        chapter_id = memory.get("chapter_id")
        if chapter_id:
            by_chapter.setdefault(chapter_id, []).append(memory)

    for chapter in chapters:
        assigned = by_chapter.get(chapter["id"], [])
        assigned.sort(key=lambda m: (m.get("position_in_chapter") is None, m.get("position_in_chapter") or 0))
        chapter["memories"] = assigned

    return chapters


def set_organization_job_status(
    memoir_id: str, job_status: str, error_message: Optional[str] = None, started: bool = False
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {"organization_status": job_status}
    if started:
        payload["organization_started_at"] = now
        payload["organization_completed_at"] = None
        payload["organization_error_message"] = None
    if job_status in ("ready", "failed"):
        payload["organization_completed_at"] = now
    if error_message is not None:
        payload["organization_error_message"] = error_message
    elif job_status == "ready":
        payload["organization_error_message"] = None
    supabase_admin.table("memoir").update(payload).eq("id", memoir_id).execute()


def fetch_archive_index(memoir_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Chapter/memory metadata only -- titles, summaries, dates.

    Deliberately excludes memory body_text and transcript content so the chat
    feature's context stays a table of contents, not the memoir's actual words.
    """
    chapters_res = (
        supabase_admin.table("chapter")
        .select("id, title, summary, sort_order")
        .eq("memoir_id", memoir_id)
        .order("sort_order")
        .execute()
    )
    memories_res = (
        supabase_admin.table("memory")
        .select("id, title, occurred_start, chapter_id")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return {"chapters": chapters_res.data or [], "memories": memories_res.data or []}


def fetch_archive_context(memoir_id: str) -> str:
    """
    Renders the archive as a text-only table of contents for the chat feature.

    Lives here rather than in the domain service because it is a data-shaping
    concern: the domain layer decides *whether* the chat may run and *with what
    history*, and the repository decides what the archive looks like as text.

    Chapter titles, summaries and memory titles/dates only. No memory body text
    and no transcript content — the chat's context is a contents page, not the
    memoir's actual words.
    """
    raw = fetch_archive_index(memoir_id)
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
                lines.append(
                    f"  - [{memory.get('occurred_start') or 'Undated'}] "
                    f"{memory.get('title') or 'Untitled'}"
                )
        else:
            lines.append("  (no memories assigned yet)")

    unassigned = [m for m in memories if not m.get("chapter_id")]
    if unassigned:
        lines.append("\nUnassigned memories:")
        for memory in unassigned:
            lines.append(f"  - {memory.get('title') or 'Untitled'}")

    return "\n".join(lines)


def fetch_organization_job_status(memoir_id: str) -> Optional[dict]:
    res = (
        supabase_admin.table("memoir")
        .select("organization_status, organization_error_message, organization_started_at, organization_completed_at")
        .eq("id", memoir_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]