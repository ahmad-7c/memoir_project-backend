"""
@file integrations/organization_repository.py
@description Data access layer for AI chapter organization: fetching
memories for the model, persisting proposed chapters, owner-driven manual
edits, and organize-job status tracking on the memoir row.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from src.integrations.supabase_client import supabase_admin


def fetch_memories_for_ai(memoir_id: str) -> List[Dict[str, Any]]:
    """
    Fetches finalized memories for the memoir. Only 'submitted' memories are
    organized -- 'draft' is the default/incomplete state a memory sits in
    while its author is still writing it (schemas/memory.py's status
    literal, matching the public.memory_status Postgres enum; the public
    share view uses this same 'submitted' filter in share_repository.py).
    Organizing drafts would group unfinished text the author hasn't chosen
    to submit yet.
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


def fetch_existing_chapter_ids(memoir_id: str) -> List[str]:
    """AI-authored, not-yet-owner-edited chapters -- the ones a fresh run may replace."""
    res = (
        supabase_admin.table("chapter")
        .select("id")
        .eq("memoir_id", memoir_id)
        .eq("edited_by_owner", False)
        .execute()
    )
    return [row["id"] for row in (res.data or [])]


def insert_chapter(memoir_id: str, title: str, sort_order: int) -> str:
    res = (
        supabase_admin.table("chapter")
        .insert(
            {
                "memoir_id": memoir_id,
                "title": title,
                "sort_order": sort_order,
                "created_by": "ai",
                "edited_by_owner": False,
            }
        )
        .execute()
    )
    if not res.data:
        raise RuntimeError("Failed to insert chapter row.")
    return res.data[0]["id"]


def delete_chapters(chapter_ids: List[str]) -> None:
    if not chapter_ids:
        return
    supabase_admin.table("chapter").delete().in_("id", chapter_ids).execute()


def set_memory_chapter(
    memory_id: str, memoir_id: str, chapter_id: Optional[str], occurred_precision: Optional[str] = None
) -> None:
    update_payload: Dict[str, Any] = {"chapter_id": chapter_id}
    if occurred_precision is not None:
        update_payload["occurred_precision"] = occurred_precision
    supabase_admin.table("memory").update(update_payload).eq("id", memory_id).eq("memoir_id", memoir_id).execute()


def fetch_memory_chapter_snapshot(memory_ids: List[str], memoir_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Captures each memory's current chapter_id/occurred_precision before an
    organize run touches it, so a failed run can restore exactly what was
    there before (R7: a partial result must never be left in place).
    """
    if not memory_ids:
        return {}
    res = (
        supabase_admin.table("memory")
        .select("id, chapter_id, occurred_precision")
        .in_("id", memory_ids)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    return {row["id"]: row for row in (res.data or [])}


def update_chapter_in_db(chapter_id: str, memoir_id: str, title: str = None, summary: str = None) -> dict:
    """Updates a chapter's owner-facing text and locks it from future AI overwrites."""
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chapter not found or does not belong to this memoir.")
    return res.data[0]


def fetch_chapter_ids_for_memoir(memoir_id: str) -> List[str]:
    res = supabase_admin.table("chapter").select("id").eq("memoir_id", memoir_id).execute()
    return [row["id"] for row in (res.data or [])]


def reorder_chapters_in_db(memoir_id: str, order: List[Dict[str, Any]]) -> List[dict]:
    """Applies a new sort_order to each listed chapter. Caller must already have
    verified every chapter_id belongs to this memoir."""
    updated = []
    for entry in order:
        res = (
            supabase_admin.table("chapter")
            .update({"sort_order": entry["sort_order"]})
            .eq("id", entry["chapter_id"])
            .eq("memoir_id", memoir_id)
            .execute()
        )
        if res.data:
            updated.append(res.data[0])
    return updated


def move_memory_in_db(memory_id: str, memoir_id: str, new_chapter_id: str) -> dict:
    """Relocates a memory to a new chapter, verifying the target belongs to this memoir."""
    chapter_check = (
        supabase_admin.table("chapter").select("id").eq("id", new_chapter_id).eq("memoir_id", memoir_id).execute()
    )
    if not chapter_check.data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target chapter: it does not belong to this memoir."
        )

    res = (
        supabase_admin.table("memory")
        .update({"chapter_id": new_chapter_id})
        .eq("id", memory_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found or does not belong to this memoir.")
    return res.data[0]


def fetch_chapters_with_memories(memoir_id: str) -> List[dict]:
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
        .select("id, title, body_text, occurred_start, occurred_precision, chapter_id")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    memories = memories_res.data or []

    for chapter in chapters:
        chapter["memories"] = [m for m in memories if m.get("chapter_id") == chapter["id"]]

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
    Chapter/memory metadata only -- titles, summaries, dates -- deliberately
    excludes memory body_text and transcript content so the chat feature's
    context stays a table-of-contents, not the memoir's actual words.
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


def fetch_organization_job_status(memoir_id: str) -> Optional[dict]:
    res = (
        supabase_admin.table("memoir")
        .select("organization_status, organization_error_message, organization_started_at, organization_completed_at")
        .eq("id", memoir_id)
        .execute()
    )
    return res.data[0] if res.data else None
