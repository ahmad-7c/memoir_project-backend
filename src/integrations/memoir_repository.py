"""
@file memoir_repository.py
@description Data access layer adapter module handling direct Supabase queries 
and persistence for user accounts, memoirs, and memoir participant roles.
"""

from src.integrations.supabase_client import supabase_admin


def fetch_user_account(user_id: str):
    """
    Fetches user account profile details from the database.

    Args:
        user_id (str): The unique identifier of the user account.

    Returns:
        Any: The database query result containing profile records.
    """
    return supabase_admin.table("user_account").select("full_name, email").eq("id", user_id).execute()


def provision_user_account(user_id: str, email: str, full_name: str):
    """
    Self-heals a missing user_account row (see MemoirService.create_memoir --
    the auth.users provisioning trigger doesn't reliably fire for every
    signup). on_conflict + ignore-duplicates makes this a safe no-op if the
    trigger actually did create the row moments after this check ran.
    """
    return (
        supabase_admin.table("user_account")
        .upsert({"id": user_id, "email": email, "full_name": full_name})
        .execute()
    )


def fetch_memoir_status(memoir_id: str):
    """
    Fetches just the status of a memoir container, for immutability checks.

    Args:
        memoir_id (str): The memoir container ID.

    Returns:
        Any: The database query result containing a single status field.
    """
    return supabase_admin.table("memoir").select("id, status").eq("id", memoir_id).execute()


def insert_memoir(memoir_data: dict):
    """
    Inserts a new root memoir container record into the database.

    Args:
        memoir_data (dict): The dictionary containing validated memoir properties.

    Returns:
        Any: The database response object containing the inserted memoir record.
    """
    return supabase_admin.table("memoir").insert(memoir_data).execute()


def insert_memoir_participant(participant_data: dict):
    """
    Registers a user as a participant in a memoir container.

    Args:
        participant_data (dict): The dictionary containing participant mapping data.

    Returns:
        Any: The database response object from the participant insertion.
    """
    return supabase_admin.table("memoir_participant").insert(participant_data).execute()

def delete_memoir_record(memoir_id: str):
    """Deletes an orphan memoir during a failed transaction rollback."""
    return supabase_admin.table("memoir").delete().eq("id", memoir_id).execute()


def fetch_memoir_by_id(memoir_id: str):
    """Fetches the full memoir record by ID."""
    return supabase_admin.table("memoir").select("*").eq("id", memoir_id).execute()


def publish_memoir_record(memoir_id: str):
    """
    Flips a memoir to 'published' and stamps published_at. Only ever called
    after MemoirService.publish_memoir has verified ownership and confirmed
    it isn't already published.
    """
    from datetime import datetime, timezone
    return (
        supabase_admin.table("memoir")
        .update({"status": "published", "published_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", memoir_id)
        .execute()
    )


def fetch_memoirs_for_user(user_id: str):
    """
    Lists every memoir the user is an active (non-removed) participant of, via
    the memoir_participant join table -- needed so a returning user's frontend
    session can resolve their existing memoir instead of only ever seeing one
    right after create_memoir returns it.
    """
    return (
        supabase_admin.table("memoir_participant")
        .select("memoir(*)")
        .eq("user_id", user_id)
        .is_("removed_at", "null")
        .execute()
    )