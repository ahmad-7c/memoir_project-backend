"""
@file integrations/proposal_repository.py
@description Server-side persistence for owner-reviewed organization proposals
and the action audit trail (alembic revision e7a41c5b9f32).

This module exists because of a specific failure mode. The reference
implementation's apply endpoint accepted the entire chapter structure as a
request body:

    class ChapterApplyPayload(BaseModel):
        chapters: List[Dict[str, Any]]

...and wrote it to the database with no ownership check and no memoir_id scope
on the per-memory update. Any authenticated user could therefore re-point
another family's memories into their own memoir.

The structural fix is to invert who holds the plan: the server persists a
validated proposal and the client holds only its id. A confirm request carries
an id and nothing else, so there is nothing in the request body that could
influence what gets written — which means there is nothing left to validate.

Every read here takes `memoir_id` and scopes by it. `supabase_admin` bypasses
Postgres RLS, so these filters are the only thing preventing cross-tenant reads.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.integrations.supabase_client import supabase_admin

logger = logging.getLogger(__name__)

PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPLYING = "applying"
PROPOSAL_STATUS_APPLIED = "applied"
PROPOSAL_STATUS_SUPERSEDED = "superseded"
PROPOSAL_STATUS_EXPIRED = "expired"

# A proposal older than this is stale: the memoir has almost certainly gained
# or lost memories since it was produced, and applying a plan built against a
# different set of ids would group the wrong things.
PROPOSAL_TTL_HOURS = 24

# Postgres SQLSTATE for unique_violation. Matched on the code, never on the
# message text: the message includes the index name and the offending value, so
# substring-matching it is both fragile and a habit worth not forming.
_PG_UNIQUE_VIOLATION = "23505"

# Rows a single fetch of the audit trail may return. Bounded so a memoir with a
# long history cannot turn one request into an unbounded read.
MAX_HISTORY_ROWS = 200


def save_proposal(
    memoir_id: str,
    payload: Dict[str, Any],
    *,
    source: str = "organize",
    summary_line: str = "",
    source_status: Optional[str] = None,
) -> str:
    """
    Persists a validated plan and returns its id.

    Any earlier pending proposal for this memoir is marked superseded first, so
    there is never more than one live proposal to confirm. Without that, an owner
    could confirm a stale proposal after reviewing a newer one.

    That supersede-then-insert sequence is two round-trips and therefore racy:
    two concurrent organize runs can each find nothing pending and each insert.
    The database closes that hole with `uq_organization_proposal_one_pending`, a
    partial unique index over `memoir_id WHERE status = 'pending'`. So a lost race
    arrives here as a unique violation rather than as two confirmable plans, and
    is resolved by superseding again and retrying once. Retrying exactly once is
    deliberate — if it fails a second time the cause is not contention and
    retrying again would only turn a bug into a hang.
    """
    for attempt in range(2):
        _supersede_pending(memoir_id)

        try:
            res = (
                supabase_admin.table("organization_proposal")
                .insert(
                    {
                        "memoir_id": memoir_id,
                        "status": PROPOSAL_STATUS_PENDING,
                        "payload": payload,
                        "source": source,
                        "summary_line": summary_line[:500],
                        "source_status": source_status,
                    }
                )
                .execute()
            )
        except Exception as exc:
            if not _is_unique_violation(exc) or attempt == 1:
                raise
            logger.info(
                "organization proposal insert lost the one-pending race (memoir_id=%s); retrying",
                memoir_id,
            )
            continue

        if not res.data:
            raise RuntimeError("Failed to persist organization proposal.")
        return res.data[0]["id"]

    # Unreachable: the loop either returns or raises. Present rather than implied,
    # because a silent fall-through here would return None as an id.
    raise RuntimeError("Failed to persist organization proposal after retry.")


def _supersede_pending(memoir_id: str) -> None:
    """
    Retires every live proposal for a memoir.

    Scoped to the two reclaimable live states, not to `pending` alone. A row
    stranded in `applying` by a process that died mid-apply is exactly as dead a
    proposal as one nobody confirmed, and leaving it in place would permanently
    block every future organize run for this memoir behind the unique index.
    """
    supabase_admin.table("organization_proposal").update(
        {"status": PROPOSAL_STATUS_SUPERSEDED}
    ).eq(
        "memoir_id", memoir_id
    ).in_(
        "status", [PROPOSAL_STATUS_PENDING, PROPOSAL_STATUS_APPLYING]
    ).execute()


def claim_proposal(proposal_id: str, memoir_id: str) -> Optional[Dict[str, Any]]:
    """
    Takes exclusive ownership of a proposal before any chapter is written.

    This is a compare-and-swap, not a read followed by a write. The update
    carries `.eq("status", "pending")`, so two concurrent confirmations of the
    same proposal produce two candidate updates and Postgres serializes them:
    exactly one matches a row and returns it, the other matches nothing and gets
    `None`. A read-then-write would let both callers see `pending` and both write
    the full chapter set, leaving the memoir with duplicate chapters and an audit
    trail showing a single apply.

    The `memoir_id` scope is not redundant with the status filter. Without it,
    proposal ids are guessable enough to probe and a caller could claim another
    family's proposal.

    Returns the claimed row, or None if the proposal is gone, not pending, or
    belongs to a different memoir — the caller cannot distinguish these and does
    not need to; they all mean "not yours to apply".
    """
    res = (
        supabase_admin.table("organization_proposal")
        .update({"status": PROPOSAL_STATUS_APPLYING})
        .eq("id", proposal_id)
        .eq("memoir_id", memoir_id)
        .eq("status", PROPOSAL_STATUS_PENDING)
        .select("id, status, payload, source, source_status, created_at")
        .execute()
    )
    return (res.data or [None])[0]


def release_proposal(proposal_id: str, memoir_id: str) -> None:
    """
    Returns a claimed proposal to `pending` after a failed apply.

    Scoped to `applying` so it cannot resurrect a proposal another caller has
    since taken. If the failure was severe enough that retrying is unsafe, expire
    it instead — but a partial apply left the memoir mid-state, so the proposal
    being confirmable again is the lesser problem and the owner is the one who
    decides.
    """
    supabase_admin.table("organization_proposal").update(
        {"status": PROPOSAL_STATUS_PENDING}
    ).eq("id", proposal_id).eq("memoir_id", memoir_id).eq(
        "status", PROPOSAL_STATUS_APPLYING
    ).execute()


def fetch_proposal(proposal_id: str, memoir_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetches one proposal, scoped to the memoir.

    Scoped, not just filtered by id: a proposal id from another memoir returns
    None here, so it becomes a 404 rather than a cross-tenant read.
    """
    res = (
        supabase_admin.table("organization_proposal")
        .select("id, memoir_id, status, payload, source, summary_line, source_status, created_at, applied_at")
        .eq("id", proposal_id)
        .eq("memoir_id", memoir_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def fetch_pending_proposal(memoir_id: str) -> Optional[Dict[str, Any]]:
    """The current live proposal for a memoir, if there is one."""
    res = (
        supabase_admin.table("organization_proposal")
        .select("id, status, payload, source, summary_line, created_at")
        .eq("memoir_id", memoir_id)
        .eq("status", PROPOSAL_STATUS_PENDING)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def mark_proposal_expired(proposal_id: str, memoir_id: str) -> None:
    """Retires a proposal that aged past PROPOSAL_TTL_HOURS."""
    supabase_admin.table("organization_proposal").update(
        {"status": PROPOSAL_STATUS_EXPIRED}
    ).eq("id", proposal_id).eq("memoir_id", memoir_id).execute()


def mark_proposal_applied(proposal_id: str, memoir_id: str, applied_by_user_id: str) -> None:
    supabase_admin.table("organization_proposal").update(
        {"status": PROPOSAL_STATUS_APPLIED, "applied_at": datetime.now(timezone.utc).isoformat(),
         "applied_by_user_id": applied_by_user_id}
    ).eq("id", proposal_id).eq("memoir_id", memoir_id).eq(
        "status", PROPOSAL_STATUS_APPLYING
    ).execute()


def is_proposal_expired(proposal: Dict[str, Any]) -> bool:
    """
    True if the proposal is too old to safely apply.

    A plan built against a set of memory ids is only valid while that set is
    unchanged. Rather than re-deriving the whole set to check, age is used as a
    conservative proxy — cheap, and errs toward asking the owner to regenerate
    rather than toward applying a stale grouping.

    Applies to `applying` rows as well as `pending` ones. There is no heartbeat
    for a claim, so the only evidence that a stuck apply is actually stuck is
    that it has been stuck for longer than any legitimate apply could take; a
    stranded row would otherwise hold the one-pending slot forever.

    A missing or unparseable timestamp is treated as expired. Fail closed: the
    alternative is treating an unreadable row as fresh, which is how a plan built
    against a set of memories that no longer exists gets applied.

    Naive timestamps (no tzinfo) are treated as UTC, which is what the column
    actually stores. Previously a naive value produced a negative age and read
    as fresh forever.
    """
    created_raw = proposal.get("created_at")
    if not created_raw:
        return True

    try:
        created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
    except ValueError:
        return True

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    return age_hours > PROPOSAL_TTL_HOURS


def record_action(
    memoir_id: str,
    actor_user_id: str,
    action_type: str,
    *,
    proposal_id: Optional[str] = None,
    target_ids: Optional[List[str]] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Appends to the audit trail.

    `actor_user_id` is always resolved from the authenticated user server-side.
    Never pass a value that came from a request body — this table's entire value
    is that it records who actually did something.

    `target_ids` and `detail` are passed as Python objects, not `json.dumps`
    strings. Both columns are `jsonb`, and pre-serialising writes a JSON *string
    scalar* into them rather than an array or object — so every reader then has
    to guess whether it got a list or a string. PostgREST serialises the request
    body itself; double-encoding is the bug, not the fix.
    """
    try:
        supabase_admin.table("organization_action_audit").insert(
            {
                "memoir_id": memoir_id,
                "actor_user_id": actor_user_id,
                "action_type": action_type,
                "proposal_id": proposal_id,
                "target_ids": target_ids or [],
                "detail": detail or {},
            }
        ).execute()
    except Exception:
        # Audit failure must not roll back or mask the user's actual action.
        # The action already happened; failing the request here would report a
        # failure for something that succeeded, which is worse than a gap in
        # the audit trail. Worth monitoring separately.
        logger.exception(
            "Failed to record audit entry for memoir_id=%s action=%s", memoir_id, action_type
        )


def fetch_action_history(memoir_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    The audit trail for a memoir, newest first.

    `limit` is clamped to MAX_HISTORY_ROWS. An unbounded audit read is an
    unbounded read, and a memoir edited daily for years is not hypothetical.
    """
    capped = max(1, min(int(limit), MAX_HISTORY_ROWS))

    res = (
        supabase_admin.table("organization_action_audit")
        .select("id, action_type, actor_user_id, target_ids, detail, created_at")
        .eq("memoir_id", memoir_id)
        .order("created_at", desc=True)
        .limit(capped)
        .execute()
    )

    history = []
    for row in res.data or []:
        history.append(
            {
                "id": row["id"],
                "action_type": row["action_type"],
                "actor_user_id": row["actor_user_id"],
                "target_ids": _safe_json_list(row.get("target_ids")),
                "detail": _safe_json_dict(row.get("detail")),
                "created_at": row.get("created_at"),
            }
        )
    return history


def _is_unique_violation(exc: BaseException) -> bool:
    """
    True if this is a Postgres unique_violation.

    postgrest surfaces the SQLSTATE in several places depending on whether the
    server returned a structured error or an HTML/text body from a proxy in
    front of it, so all of them are checked. None of this inspects the message
    text for a substring.
    """
    code = getattr(getattr(exc, "code", None), "__str__", lambda: None)()
    if code == _PG_UNIQUE_VIOLATION:
        return True

    # APIError / PostgrestAPIError path: a dict of Postgres error fields.
    for attr in ("code", "sqlstate", "sql_state"):
        value = getattr(exc, attr, None)
        if value == _PG_UNIQUE_VIOLATION:
            return True

    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details.get("code") == _PG_UNIQUE_VIOLATION:
        return True

    # Some versions nest it under .json / .args.
    for container in (getattr(exc, "json", None), getattr(exc, "args", None)):
        candidates = container if isinstance(container, list) else [container]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("code") == _PG_UNIQUE_VIOLATION:
                return True

    return False


def _safe_json_list(raw: Any) -> List[str]:
    """
    Normalises a jsonb array column to a list of strings.

    Handles both shapes this column has held in practice: a real JSONB array, and
    a bare string, from rows written before the double-encoding bug was fixed.
    Anything unrecognised returns empty rather than raising — an audit row that
    cannot be rendered must not take down the history endpoint.
    """
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _safe_json_dict(raw: Any) -> Dict[str, Any]:
    """Normalises a jsonb object column, tolerating the pre-fix string encoding."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
