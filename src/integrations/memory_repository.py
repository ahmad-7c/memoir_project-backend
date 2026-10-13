"""
@file memory_repository.py
@description Data access layer adapter module handling direct Supabase queries 
and persistence operations for memories, participant permissions, and media junctions,
enforcing strict multi-tenant memoir_id isolation.
"""

from src.integrations.supabase_client import supabase_admin


def insert_memory(memory_data: dict):
    """
    Persists a new memory record into the database table.
    """
    return supabase_admin.table("memory").insert(memory_data).execute()


def insert_memory_media(link_records: list):
    """
    Links media asset IDs to a specific memory record via the junction table.
    """
    return supabase_admin.table("memory_media").insert(link_records).execute()


def fetch_memoir_feed_records(memoir_id: str, limit: int = 20, offset: int = 0):
    """
    Retrieves paginated active memories with explicit columns and embedded media assets.
    """
    end_index = offset + limit - 1
    return supabase_admin.table("memory") \
        .select(
            "id, memoir_id, author_participant_id, title, body_text, status, "
            "occurred_start, occurred_end, occurred_precision, date_source, created_at, "
            "chapter_id, memory_media(media_asset(*))"
        ) \
        .eq("memoir_id", memoir_id) \
        .is_("deleted_at", "null") \
        .order("created_at", desc=True) \
        .range(offset, end_index) \
        .execute()
        
def fetch_memory_by_id(memory_id: str, memoir_id: str):
    """
    Fetches status metadata for a specific memory record strictly scoped by memoir_id 
    to prevent cross-tenant data probing.
    """
    return supabase_admin.table("memory") \
        .select("memoir_id, status") \
        .eq("id", memory_id) \
        .eq("memoir_id", memoir_id) \
        .execute()

def update_memory_record(memory_id: str, memoir_id: str, fields: dict):
    """
    Partial update of one memory, scoped by memoir_id as well as memory_id.

    Both ids are in the predicate deliberately: the caller's participation is
    verified against the MEMOIR, so the write must be constrained to that same
    memoir or a valid participant of memoir A could edit a memory in memoir B
    by supplying its id.
    """
    return (
        supabase_admin.table("memory")
        .update(fields)
        .eq("id", memory_id)
        .eq("memoir_id", memoir_id)
        .is_("deleted_at", "null")
        .execute()
    )


def fetch_memory_media_ids(memory_id: str):
    """Media asset ids already linked to a memory -- used to make attach idempotent."""
    res = (
        supabase_admin.table("memory_media")
        .select("media_asset_id")
        .eq("memory_id", memory_id)
        .execute()
    )
    return {str(r["media_asset_id"]) for r in (res.data or [])}


def delete_memory_record(memory_id: str, memoir_id: str):
    """
    Deletes a memory record permanently from the database, strictly scoped by 
    both ID and memoir_id to prevent TOCTOU race conditions.
    """
    return supabase_admin.table("memory") \
        .delete() \
        .eq("id", memory_id) \
        .eq("memoir_id", memoir_id) \
        .execute()
        
def verify_media_assets_belong_to_memoir(memoir_id: str, media_asset_ids: list[str]):
    """
    Verifies that a list of media asset IDs all mathematically belong to the target memoir container.
    """
    res = supabase_admin.table("media_asset") \
        .select("id") \
        .eq("memoir_id", memoir_id) \
        .in_("id", media_asset_ids) \
        .execute()
    return res.data or []

def soft_delete_memory_record(memory_id: str, memoir_id: str):
    """
    Performs a soft-delete on a memory record by setting the deleted_at timestamp, 
    making deletions reversible and preserving data integrity.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return supabase_admin.table("memory") \
        .update({"deleted_at": now}) \
        .eq("id", memory_id) \
        .eq("memoir_id", memoir_id) \
        .execute()

def fetch_media_asset_record(media_asset_id: str):
    """
    Fetches the full media asset record (including storage_key and kind) by its ID.
    """
    res = supabase_admin.table("media_asset") \
        .select("*") \
        .eq("id", media_asset_id) \
        .maybe_single() \
        .execute()
    return res.data if res else None

def upsert_transcript_record(transcript_payload: dict):
    """
    Saves or updates the AI transcript in the database,
    keeping DB operations isolated from integration logic.
    """
    return supabase_admin.table("transcript").upsert(transcript_payload).execute()

def update_media_asset_transcription_status(media_asset_id: str, transcription_status: str):
    """
    Updates the transcription lifecycle status on a media asset row.
    """
    return supabase_admin.table("media_asset") \
        .update({"transcription_status": transcription_status}) \
        .eq("id", media_asset_id) \
        .execute()