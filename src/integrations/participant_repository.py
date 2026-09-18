"""
@file participant_repository.py
@description Single source of truth for resolving a memoir participant record.
Used everywhere a caller's membership in a memoir needs to be verified.
"""

from src.integrations.supabase_client import supabase_admin


def fetch_participant(memoir_id: str, user_id: str):
    """
    Queries the database to verify if a user is an authorized active (non-removed)
    participant of a memoir.

    Args:
        memoir_id (str): The unique identifier of the target memoir.
        user_id (str): The unique identifier of the user.

    Returns:
        Any: The database query result object containing participant data.
    """
    return supabase_admin.table("memoir_participant") \
        .select("*") \
        .eq("memoir_id", memoir_id) \
        .eq("user_id", user_id) \
        .is_("removed_at", "null") \
        .execute()
