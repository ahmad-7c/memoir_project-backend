"""
@file profile_repository.py
@description Data access for the owner profile page: account details plus a
per-memoir summary (status, memory count, cover image) for every memoir the
caller owns.
"""

from typing import Any, Dict, List

from src.integrations.supabase_client import supabase_admin
from src.integrations import storage_adapter


def fetch_account(user_id: str) -> Dict[str, Any]:
    """Full_name/email/subscription_status for the profile header."""
    res = (
        supabase_admin.table("user_account")
        .select("id, full_name, email, subscription_status")
        .eq("id", user_id)
        .execute()
    )
    return res.data[0] if res.data else {}


def fetch_owned_memoirs(user_id: str) -> List[Dict[str, Any]]:
    """
    Every memoir this user owns (not merely participates in -- a profile page
    lists what belongs to the account, not everything it can see).
    """
    res = (
        supabase_admin.table("memoir_participant")
        .select("memoir(*)")
        .eq("user_id", user_id)
        .eq("role", "owner")
        .is_("removed_at", "null")
        .execute()
    )
    return [row["memoir"] for row in (res.data or []) if row.get("memoir")]


def fetch_memory_counts(memoir_ids: List[str]) -> Dict[str, int]:
    """
    One query for every memoir's saved (submitted, non-deleted) memory count,
    instead of one query per memoir card -- the same N+1 shape already flagged
    elsewhere in this codebase (memory feed's transcript fetch) is avoided here
    from the start.
    """
    if not memoir_ids:
        return {}
    res = (
        supabase_admin.table("memory")
        .select("memoir_id")
        .in_("memoir_id", memoir_ids)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    counts: Dict[str, int] = {mid: 0 for mid in memoir_ids}
    for row in res.data or []:
        mid = str(row["memoir_id"])
        counts[mid] = counts.get(mid, 0) + 1
    return counts


def fetch_cover_image_urls(cover_media_ids: List[str]) -> Dict[str, str]:
    """Maps media_asset.id -> a signed playback URL, for use as a memoir's cover image."""
    if not cover_media_ids:
        return {}
    res = (
        supabase_admin.table("media_asset")
        .select("id, storage_key")
        .in_("id", cover_media_ids)
        .execute()
    )
    urls: Dict[str, str] = {}
    for row in res.data or []:
        storage_key = row.get("storage_key")
        if not storage_key:
            continue
        url = storage_adapter.create_playback_url(storage_key)
        if url:
            urls[str(row["id"])] = url
    return urls
