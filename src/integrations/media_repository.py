"""
@file media_repository.py
@description Data access layer adapter module handling direct Supabase queries 
for participant authorizations and media asset metadata persistence.
"""

from src.integrations.supabase_client import supabase
from src.integrations.supabase_client import supabase_admin


def insert_media_metadata(media_data: dict):
    """
    Persists a new media asset metadata record into the database.

    Args:
        media_data (dict): The dictionary containing validated media asset attributes.

    Returns:
        Any: The database response object containing the inserted media record.
    """
    return supabase_admin.table("media_asset").insert(media_data).execute()

def fetch_media_asset(media_id: str, memoir_id: str):
    """
    Fetches one media asset, scoped to the memoir it must belong to -- used
    to verify a client-supplied media_id (e.g. setting a cover photo) isn't
    pointing at an asset from a memoir the caller doesn't own.
    """
    res = (
        supabase_admin.table("media_asset")
        .select("id, kind, memoir_id")
        .eq("id", media_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    return res.data[0] if res.data else None


def check_existing_media_by_checksum(memoir_id: str, checksum: str):
    """
    Checks if a media asset with the exact same checksum already exists in this memoir 
    to prevent duplicate ghost uploads.
    """
    if not checksum:
        return None
    res = supabase.table("media_asset") \
        .select("id, storage_key") \
        .eq("memoir_id", memoir_id) \
        .eq("checksum_sha256", checksum) \
        .execute()
    return res.data[0] if res.data else None