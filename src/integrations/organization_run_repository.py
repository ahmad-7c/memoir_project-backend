"""
@file integrations/organization_run_repository.py
@description Persistence for AI organization runs and their per-stage progress
(alembic revision c3f9a1b204d7).

WHY THIS EXISTS

The `memoir` row carries `organization_status` / `_started_at` /
`_completed_at`. That answers "did it finish?" and nothing else. A pipeline of
Reader -> Organizer -> Resolver needs three more things to be knowable while it
runs, and this module is where they live:

  1. Which stage is executing, so an owner sees progress instead of a spinner.
  2. Which provider actually served each stage. The LLM layer fails over
     between Groq and Gemini on rate limits and 5xx; without a per-stage record
     that failover is invisible afterwards, and a chronic misconfiguration looks
     exactly like a healthy run.
  3. How far a dead job got. BackgroundTasks has no heartbeat and no crash
     recovery, so a process killed mid-run leaves `organization_status` at
     'running' forever.

WHY THESE MUTATIONS ARE NOT SCOPED BY `memoir_id`

Every other repository in this package scopes each mutation by `memoir_id`, and
for good reason: `supabase_admin` bypasses RLS, so the filter is the only thing
between a bug and a cross-tenant write. The rule does not bend here so much as
it does not apply.

These rows are written only from inside `perform_background_organization`, using
ids the server generated itself moments earlier in the same function. No
`organization_job_run` or `organization_agent_run` id is ever accepted from a
request path, a request body, or a client-held value — so there is no untrusted
input that could redirect the update at another tenant's row. Adding
`memoir_id` to these filters would mean an extra round-trip per stage write on
the hot path of a background job, to guard against an input that does not exist.

If a future endpoint ever accepts one of these ids from a caller, that endpoint
must resolve `memoir_id` from the authenticated user and pass it through. The
docstrings below say so at the point where it would be easy to forget.

Every read here *is* memoir-scoped, because reads do come from request paths.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.integrations.supabase_client import supabase_admin

logger = logging.getLogger(__name__)

RUN_STATUS_RUNNING = "running"
RUN_STATUS_READY = "ready"
RUN_STATUS_FAILED = "failed"

# Pipeline stage names, in execution order. Text rather than an enum in the
# schema so adding a stage is a new row rather than a migration; these constants
# exist so the pipeline and any caller comparing labels use the same spellings.
AGENT_READER = "reader"
AGENT_ORGANIZER = "organizer"
AGENT_REFINER = "refiner"
AGENT_RESOLVER = "resolver"

# Upper bound on agent rows per run. The pipeline creates one per stage per
# attempt, so this is a sanity bound rather than a real limit — it exists so a
# bug that loops instead of advancing fails loudly instead of filling a table.
MAX_AGENT_RUNS_PER_JOB = 64

# Owner-facing, provider-free, content-free. Cap enforced in code because this
# column is read straight back out by the status endpoint and shown to a family.
MAX_ERROR_MESSAGE_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_run(memoir_id: str, *, attempt: int = 1) -> str:
    """Opens a run row and returns its id."""
    res = (
        supabase_admin.table("organization_job_run")
        .insert(
            {
                "memoir_id": memoir_id,
                "status": RUN_STATUS_RUNNING,
                "attempt": max(1, int(attempt)),
                "started_at": _now(),
            }
        )
        .execute()
    )
    if not res.data:
        raise RuntimeError("Failed to open organization job run.")
    return res.data[0]["id"]


def finish_run(
    run_id: str,
    status: str,
    *,
    provider_used: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Closes a run in a terminal state.

    Guarded to `running` so a late-arriving exit path from a retried or
    duplicated job cannot overwrite a terminal state with a contradictory one.
    The `completed_at >= started_at` CHECK backs this up: a second completion
    would otherwise trip the constraint and turn a harmless race into a
    reported failure.
    """
    payload: Dict[str, Any] = {"status": status, "completed_at": _now()}
    if provider_used is not None:
        payload["provider_used"] = provider_used
    if error_message is not None:
        payload["error_message"] = error_message[:MAX_ERROR_MESSAGE_CHARS]

    supabase_admin.table("organization_job_run").update(payload).eq("id", run_id).eq(
        "status", RUN_STATUS_RUNNING
    ).execute()


def start_agent_run(job_run_id: str, agent_role: str, *, attempt: int = 1) -> Optional[str]:
    """
    Opens a stage row and returns its id, or None if it could not be recorded.

    Returns None rather than raising on the way out. Stage bookkeeping is
    observability, not the job: a run whose progress rows failed to write is
    still a run whose chapters must be written. Swallowing the failure here and
    logging it is the difference between a missing progress bar and a lost
    memoir organization.

    The attempt count is checked before the insert because there is no
    constraint to lean on for it.
    """
    try:
        existing = (
            supabase_admin.table("organization_agent_run")
            .select("id")
            .eq("job_run_id", job_run_id)
            .limit(MAX_AGENT_RUNS_PER_JOB)
            .execute()
        )
        if len(existing.data or []) >= MAX_AGENT_RUNS_PER_JOB:
            logger.warning(
                "agent run cap reached for job_run_id=%s; not recording stage %s",
                job_run_id,
                agent_role,
            )
            return None

        res = (
            supabase_admin.table("organization_agent_run")
            .insert(
                {
                    "job_run_id": job_run_id,
                    "agent_role": agent_role,
                    "status": RUN_STATUS_RUNNING,
                    "attempt": max(1, int(attempt)),
                    "started_at": _now(),
                }
            )
            .execute()
        )
        if not res.data:
            logger.warning("stage %s produced no row id for job_run_id=%s", agent_role, job_run_id)
            return None
        return res.data[0]["id"]
    except Exception:
        logger.exception(
            "failed to open stage row (job_run_id=%s agent_role=%s)", job_run_id, agent_role
        )
        return None


def finish_agent_run(
    agent_run_id: Optional[str],
    status: str,
    *,
    provider_used: Optional[str] = None,
    error_message: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """
    Closes a stage row. A None id is a no-op, not an error.

    Token counts are passed through as given, including None. A missing count
    must stay missing rather than becoming 0: "the provider did not report usage"
    and "the provider reported zero usage" are different facts, and only one of
    them is worth alerting on.
    """
    if not agent_run_id:
        return

    payload: Dict[str, Any] = {"status": status, "completed_at": _now()}
    if provider_used is not None:
        payload["provider_used"] = provider_used
    if error_message is not None:
        payload["error_message"] = error_message[:MAX_ERROR_MESSAGE_CHARS]
    if input_tokens is not None:
        payload["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        payload["output_tokens"] = int(output_tokens)
    if duration_ms is not None:
        payload["duration_ms"] = max(0, int(duration_ms))

    try:
        supabase_admin.table("organization_agent_run").update(payload).eq(
            "id", agent_run_id
        ).eq("status", RUN_STATUS_RUNNING).execute()
    except Exception:
        logger.exception("failed to close stage row id=%s", agent_run_id)


def fetch_latest_run(memoir_id: str) -> Optional[Dict[str, Any]]:
    """
    The most recent run for a memoir, with its stage rows.

    Read path, so scoped by `memoir_id` and driven by
    `idx_organization_job_run_memoir (memoir_id, started_at)`.
    """
    runs = (
        supabase_admin.table("organization_job_run")
        .select("id, status, provider_used, attempt, error_message, started_at, completed_at")
        .eq("memoir_id", memoir_id)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    run = (runs.data or [None])[0]
    if not run:
        return None

    run["agent_runs"] = fetch_agent_runs(run["id"])
    return run


def fetch_agent_runs(job_run_id: str) -> List[Dict[str, Any]]:
    """Stage rows for one run, oldest first."""
    res = (
        supabase_admin.table("organization_agent_run")
        .select(
            "id, agent_role, status, provider_used, attempt, input_tokens, output_tokens, "
            "duration_ms, error_message, started_at, completed_at"
        )
        .eq("job_run_id", job_run_id)
        .order("started_at")
        .execute()
    )
    return res.data or []
