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


def publish_memoir_tx(memoir_id: str, owner_user_id: str, token: str):
    """
    Calls publish_memoir_tx() (migrations/a1c9e4f27b3d) -- a single Postgres
    function that flips status, stamps published_at, flags pdf_exportable,
    and creates the share link all in one transaction. See that migration's
    docstring for why this has to be a stored function rather than separate
    .table() calls: postgrest has no multi-statement transaction over HTTP,
    so three separate calls could leave a memoir published with no share link
    if the process died between them.

    Raises the underlying postgrest exception on failure -- the caller
    (MemoirService.publish_memoir) maps its SQLSTATE to the right HTTP status.
    """
    return supabase_admin.rpc(
        "publish_memoir_tx",
        {
            "p_memoir_id": memoir_id,
            "p_owner_user_id": owner_user_id,
            "p_token": token,
        },
    ).execute()


def update_memoir_comment_policy(memoir_id: str, comment_policy: str):
    """
    Used by ShareService.update_share_link's `can_comment` toggle. Writing
    through the real memoir.comment_policy column (not a separate flag on
    memoir_link) means this setting is consistent wherever comment_policy is
    read -- reader unlock, the reader-only dependency's live re-check, etc.
    """
    return (
        supabase_admin.table("memoir")
        .update({"comment_policy": comment_policy})
        .eq("id", memoir_id)
        .execute()
    )


def fetch_memoir_narrative_reviewed_at(memoir_id: str):
    """Used only by MemoirService.publish_memoir's narrative-review gate."""
    res = supabase_admin.table("memoir").select("narrative_reviewed_at").eq("id", memoir_id).execute()
    return res.data[0].get("narrative_reviewed_at") if res.data else None


def fetch_submitted_memory_count(memoir_id: str) -> int:
    """Used for a pre-flight, user-friendly check before calling publish_memoir_tx."""
    res = (
        supabase_admin.table("memory")
        .select("id", count="exact")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return res.count or 0


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