"""
@file backend/src/integrations/organization_view_repository.py
@description Read-only queries that exist to serve the AI organization review
screen.

SEPARATE FROM organization_repository.py ON PURPOSE

`organization_repository` holds the write path -- the functions that create
chapters and assign memories. This holds the reads that a *reviewer* needs,
which are a different question:

    organization_repository  "what will the plan do?"
    organization_view_repository  "what is actually there right now?"

Keeping them apart means the review screen's queries cannot drift into becoming
write paths, and it keeps the security-critical functions -- the ones that
mutate -- in one short, auditable file rather than mixed with display queries.

Every function here is read-only, and every one is `memoir_id`-scoped. An
unscoped read is not a cross-tenant *write*, but it still hands one family
another family's memories, which for this product is the worst available leak.
"""

from typing import Any, Dict, List

from src.integrations.supabase_client import supabase_admin

# Columns returned for the review list.
#
# Named explicitly, never `*`. `memory` carries no storage paths today, but it
# carries `search_tsv` (a tsvector of the full body) and internal deletion
# metadata, neither of which belongs in a response that was designed to carry a
# title and a body.
_MEMORY_COLUMNS = (
    "id, title, body_text, occurred_start, occurred_precision, "
    "kind, chapter_id, position_in_chapter, status, submitted_at, deleted_at"
)


def fetch_submitted_memories_for_review(memoir_id: str) -> List[Dict[str, Any]]:
    """
    Every submitted memory in this memoir, with its chapter assignment.

    `GET /chapters` cannot serve this: it returns chapters joined to the memories
    already assigned to them, so a memory the AI has not yet placed is invisible.
    That is exactly the set the owner most needs to see -- the "Other Memories"
    catch-all candidates -- so the review screen needs its own query.

    Drafts are excluded for the same reason the organizer excludes them: a draft
    is text the author has not chosen to submit, and organizing it would put
    unfinished writing into a published family book.

    `deleted_at IS NULL` is explicit rather than inherited from a caller. Soft
    delete being handled "three files away" is how a deleted memory ends up in
    someone's memoir.
    """
    res = (
        supabase_admin.table("memory")
        .select(_MEMORY_COLUMNS)
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .order("occurred_start", nullslast=True)
        .execute()
    )
    return res.data or []


def fetch_memory_counts_for_review(memoir_id: str) -> Dict[str, int]:
    """
    Counts for the review header, in two queries rather than one per status.

    A single grouped query would be tidier, but PostgREST cannot express
    `GROUP BY status` without an RPC, and an RPC that exists only for a page
    header is a worse trade than two cheap indexed reads.
    """
    submitted = (
        supabase_admin.table("memory")
        .select("id", count="exact")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    drafts = (
        supabase_admin.table("memory")
        .select("id", count="exact")
        .eq("memoir_id", memoir_id)
        .eq("status", "draft")
        .is_("deleted_at", "null")
        .execute()
    )

    return {
        "submitted": len(submitted.data or []),
        "drafts": len(drafts.data or []),
    }