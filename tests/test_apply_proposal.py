"""
@file tests/test_apply_proposal.py
@description Tests for `confirm_proposal` -- the write path.

Everything upstream is a proposal. This is the only place chapters are actually
written, so it carries every rule that separates "the owner reviewed this" from
"the server wrote whatever arrived":

  * claim before validate, so a double-click cannot write the chapter set twice
  * 404 when the proposal is not yours, 409 when it is yours but already handled
  * release back to `pending` on any failure, scoped so it cannot resurrect a
    proposal another caller has taken
  * re-verify membership at write time, not at creation time
  * never write `memory.body_text`

Nothing here touches a database. The repositories are replaced with recording
fakes, so the assertions are about the sequence of calls and their scoping -- the
parts a live-database test would not isolate.
"""

import pytest
from fastapi import HTTPException

from src.domain import organization_service as svc
from src.integrations import proposal_repository as proposal_repo
from tests.helpers import (
    SANDBOX_MEMOIR_ID,
    SANDBOX_MEMORY_ID,
    live_connection,
    scaffold_memoir,
    scaffold_submitted_memory,
)

A_PLAN = {
    "chapters": [
        {
            "title": "Childhood",
            "summary": "AI-authored prose.",
            "era_label": "1960s",
            "sort_order": 0,
            "memory_ids": ["m1", "m2"],
        }
    ],
    "unplaced_memory_ids": ["m3"],
}


def _pending_proposal(**overrides):
    base = {
        "id": "prop-1",
        "status": "pending",
        "payload": A_PLAN,
        "source": "organize",
        "source_status": "ready",
        "created_at": "2026-10-03T00:00:00Z",
    }
    base.update(overrides)
    return base


@pytest.fixture
def wiring(monkeypatch):
    """
    Replaces every side effect of `confirm_proposal` with a recorder.

    Returns a dict the test asserts on. Written as one fixture so a test cannot
    accidentally exercise a real repository by omitting a patch -- the default is
    "everything is recorded", and opting into the real thing is a deliberate act.
    """
    calls: dict = {
        "applied": [],
        "applied_marked": [],
        "released": [],
        "expired": [],
        "recorded": [],
    }

    # An open proposal, claimed successfully on the first attempt.
    monkeypatch.setattr(
        svc.proposal_repo,
        "claim_proposal",
        lambda proposal_id, memoir_id: _pending_proposal(),
    )
    monkeypatch.setattr(svc.proposal_repo, "is_proposal_expired", lambda proposal: False)
    monkeypatch.setattr(
        svc.repo, "fetch_submitted_memory_ids", lambda memoir_id: {"m1", "m2", "m3"}
    )
    monkeypatch.setattr(svc.proposal_repo, "mark_proposal_expired", lambda *a: calls["expired"].append(a))
    monkeypatch.setattr(
        svc.proposal_repo, "mark_proposal_applied",
        lambda *a: calls["applied_marked"].append(a),
    )
    monkeypatch.setattr(
        svc.proposal_repo, "release_proposal", lambda *a: calls["released"].append(a)
    )
    monkeypatch.setattr(
        svc.proposal_repo,
        "record_action",
        lambda *a, **kw: calls["recorded"].append((a, kw)),
    )

    # The write itself.
    monkeypatch.setattr(
        svc,
        "_apply_resolved_plan",
        lambda memoir_id, plan, *, verified_ids: calls["applied"].append(
            {"memoir_id": memoir_id, "plan": plan, "verified_ids": verified_ids}
        ),
    )
    # Not patched here: `confirm_proposal` does not call
    # `assert_memoir_editable`. The check lives at the route, and the database
    # trigger is what actually makes the invariant hold. See
    # `test_a_published_memoir_is_stopped_before_the_write`.

    return calls


MEMOIR = "memoir-1"
USER = "user-1"
PROPOSAL = "prop-1"


# ---------------------------------------------------------------------------
# The happy path, and the shape of what gets written
# ---------------------------------------------------------------------------


async def test_apply_writes_the_plan_with_the_revalidated_id_set(wiring):
    result = await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert len(wiring["applied"]) == 1
    written = wiring["applied"][0]
    assert written["memoir_id"] == MEMOIR
    # The verified set is the one re-derived NOW, not the one from creation time.
    assert written["verified_ids"] == {"m1", "m2", "m3"}
    assert result.chapters[0].title == "Childhood"
    assert result.unplaced_memory_ids == ["m3"]


async def test_apply_passes_verified_ids_as_a_keyword_only_argument(wiring):
    """
    `_apply_resolved_plan(..., *, verified_ids)` is keyword-only on purpose. It
    is the check that makes an `upsert` write safe (upsert can insert), and a
    positional parameter is one refactor away from being omitted by accident.
    """
    import inspect

    signature = inspect.signature(svc._apply_resolved_plan)
    assert (
        signature.parameters["verified_ids"].kind is inspect.Parameter.KEYWORD_ONLY
    ), "verified_ids must be keyword-only; a positional param can be silently dropped"

    # Calling it the way a future refactor might -- positionally -- must fail at
    # the signature rather than silently dropping the argument.
    with pytest.raises(TypeError):
        svc._apply_resolved_plan(MEMOIR, None, {"m1"})  # type: ignore[misc]


async def test_apply_does_not_release_a_proposal_that_succeeded(wiring):
    """
    Releasing after success would hand the proposal back to `pending`, so the
    owner could confirm the same plan a second time and get a duplicate chapter
    set.
    """
    await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert wiring["released"] == []
    # And it is moved to `applied` rather than left claimable.
    assert len(wiring["applied_marked"]) == 1


# ---------------------------------------------------------------------------
# Membership is re-verified at write time
# ---------------------------------------------------------------------------


async def test_a_memory_deleted_while_the_owner_was_reviewing_blocks_the_apply(monkeypatch, wiring):
    """
    The plan is only valid while the memory set behind it is unchanged. A memoir
    gains and loses memories while a proposal sits in the review screen, so the
    check at creation time is already stale by the time it is confirmed.
    """
    # m2 was deleted after the proposal was generated.
    monkeypatch.setattr(svc.repo, "fetch_submitted_memory_ids", lambda memoir_id: {"m1", "m3"})

    with pytest.raises(HTTPException) as exc:
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert exc.value.status_code == 409
    assert wiring["applied"] == [], "nothing may be written when the plan no longer applies"


async def test_a_memory_added_while_the_owner_was_reviewing_does_not_block_the_apply(monkeypatch, wiring):
    """
    The check is that every id IN THE PLAN still belongs to the memoir, not that
    the two sets are identical. A new memory appearing does not invalidate a
    grouping of older ones -- refusing here would make the feature unusable for
    anyone actively adding memories, which is the common case.
    """
    monkeypatch.setattr(
        svc.repo, "fetch_submitted_memory_ids", lambda memoir_id: {"m1", "m2", "m3", "m4"}
    )

    await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert len(wiring["applied"]) == 1
    assert wiring["applied"][0]["verified_ids"] == {"m1", "m2", "m3", "m4"}


async def test_an_empty_submitted_set_refuses_the_apply(monkeypatch, wiring):
    """Every memory was deleted. There is nothing to organize and nothing to write."""
    monkeypatch.setattr(svc.repo, "fetch_submitted_memory_ids", lambda memoir_id: set())

    with pytest.raises(HTTPException) as exc:
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert exc.value.status_code == 409
    assert wiring["applied"] == []


# ---------------------------------------------------------------------------
# Claim semantics: the double-click
# ---------------------------------------------------------------------------


async def test_a_lost_claim_is_404_when_the_proposal_is_not_there(monkeypatch, wiring):
    """
    The claim is a compare-and-swap, so a lost claim is ambiguous by design: it
    means either "no such proposal" or "not yours". One scoped re-read separates
    them, and the answer is 404 -- a 409 would confirm a stranger's proposal id
    exists.
    """
    monkeypatch.setattr(svc.proposal_repo, "claim_proposal", lambda *a: None)
    monkeypatch.setattr(svc.proposal_repo, "fetch_proposal", lambda *a: None)

    with pytest.raises(HTTPException) as exc:
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert exc.value.status_code == 404
    assert wiring["applied"] == []


async def test_a_lost_claim_is_409_when_the_proposal_exists_and_is_already_handled(monkeypatch, wiring):
    """
    The other half of the same ambiguity: the caller is the owner, the proposal
    exists, it simply is no longer pending. 409 tells them to regenerate, which
    is actionable -- and it is safe to give that answer because they are already
    authorized for this memoir.
    """
    monkeypatch.setattr(svc.proposal_repo, "claim_proposal", lambda *a: None)
    monkeypatch.setattr(svc.proposal_repo, "fetch_proposal", lambda *a: _pending_proposal())

    with pytest.raises(HTTPException) as exc:
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert exc.value.status_code == 409
    assert wiring["applied"] == []


async def test_a_claim_lost_to_another_caller_does_not_release_their_claim(monkeypatch, wiring):
    """
    The release is scoped to `status = 'applying'`. An unscoped release would
    reset a proposal that a *different* request had just claimed, and both would
    go on to write.
    """
    monkeypatch.setattr(svc.proposal_repo, "claim_proposal", lambda *a: None)
    monkeypatch.setattr(svc.proposal_repo, "fetch_proposal", lambda *a: _pending_proposal())

    with pytest.raises(HTTPException):
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert wiring["released"] == [], (
        "a lost claim must not release anything; the row belongs to whoever "
        "actually holds it"
    )


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_an_expired_proposal_is_expired_then_refused(monkeypatch, wiring):
    """
    A 24h TTL is a conservative proxy for "the memory set is unchanged". Rather
    than re-deriving the whole set to check, age is used -- cheap, and erring
    toward asking the owner to regenerate rather than toward applying a stale
    grouping.
    """
    monkeypatch.setattr(svc.proposal_repo, "is_proposal_expired", lambda proposal: True)

    with pytest.raises(HTTPException) as exc:
        await svc.confirm_proposal(PROPOSAL, MEMOIR, USER)

    assert exc.value.status_code == 409
    assert len(wiring["expired"]) == 1
    assert wiring["applied"] == []


# ---------------------------------------------------------------------------
# The release path, read from the repository rather than exercised
# ---------------------------------------------------------------------------


def test_the_release_is_scoped_to_the_applying_status():
    """
    Two properties of `release_proposal` that no behavioural test can observe.

    A behavioural test would have to arrange for two callers to hold the same
    proposal simultaneously and then assert on which one won -- which needs real
    concurrency, real races, and a flake rate that makes people disable the test.
    The property is structural, so it is asserted structurally.

    Why the scoping matters, in both directions:

      * Without `.eq("status", "applying")`, a failed apply resets a proposal a
        *different* request has since claimed, and both write the chapter set.
      * The expiry path raises, which triggers the same release. By then the row
        is `expired`, and the filter is what stops it becoming confirmable again
        -- a stale plan applied after its TTL is exactly what the TTL prevents.

    Asserted on the source because this is the only place the guarantee lives: the
    caller in `confirm_proposal` cannot see it, and there is no schema constraint
    behind it.
    """
    import inspect

    source = inspect.getsource(proposal_repo.release_proposal)

    assert "applying" in source, (
        "release_proposal lost its status scope; a failed apply would resurrect "
        "a proposal another caller holds, and an expired one would come back to life"
    )
    assert "memoir_id" in source, (
        "release_proposal lost its memoir_id scope; it could reset another "
        "family's proposal"
    )


def test_the_release_does_not_set_a_status_that_the_check_constraint_forbids():
    """
    `release_proposal` writes `pending`, which the CHECK constraint permits. If
    it were ever changed to an intermediate value, the write would fail at the
    database with a constraint violation instead of silently doing nothing --
    and that failure would happen inside the `except` block that is trying to
    rescue a failed apply, masking the original error.
    """
    import inspect

    source = inspect.getsource(proposal_repo.release_proposal)

    assert "pending" in source, "release_proposal must return the row to 'pending'"
    # And the value it returns to is one the constraint allows.
    assert "applying" in source, (
        "release_proposal's scope and its target value are coupled: the scope "
        "must match the status it writes, or the update silently matches nothing"
    )


# ---------------------------------------------------------------------------
# The immutability invariant, at both layers
# ---------------------------------------------------------------------------


def test_the_apply_route_checks_editability_before_writing():
    """
    `assert_memoir_editable` runs in the ROUTE, not in `confirm_proposal`.

    That placement looks like a gap, because `confirm_proposal` has two callers
    and a route-only check could be bypassed by the other. It is not a gap: the
    database trigger is the real enforcement (see the next test), and the
    application check exists to produce a clean 409 instead of a raised
    exception surfacing as a 500.

    What is asserted is the ORDER. A check that ran after the write would be
    theatre.
    """
    from pathlib import Path

    from src.api import organization as api

    lines = Path(api.__file__).read_text(encoding="utf-8").splitlines()

    route_start = next(
        i for i, line in enumerate(lines)
        if "async def apply_organization_proposal" in line
    )
    route = lines[route_start : route_start + 45]

    check_at = next((i for i, line in enumerate(route) if "assert_memoir_editable" in line), None)
    call_at = next((i for i, line in enumerate(route) if "confirm_proposal" in line), None)

    assert check_at is not None, (
        "the apply route must call assert_memoir_editable; the DB trigger is the "
        "backstop, not the primary defence"
    )
    assert call_at is not None, "the apply route no longer calls confirm_proposal"
    assert check_at < call_at, "the check must run BEFORE the write, not after"


def _write_routes_without_an_editability_check() -> list[str]:
    """
    Write routes in `api/organization.py` that never call
    `assert_memoir_editable`, with an exemption list for the ones that legitimately
    do not write memoir content.

    Root AGENTS.md invariant 4: "If you add a write path, you must call the
    first." That is a rule a codebase forgets -- which is why the Postgres trigger
    exists as the backstop. But the trigger *raises*, and an exception surfacing
    from PostgREST becomes an opaque 500. The application check exists so an owner
    editing a published memoir gets a 409 that says why.

    Parsed from source rather than enumerated by hand: a hand-written list stops
    covering new routes, which is exactly the regression this guards.
    """
    from pathlib import Path

    from src.api import organization as api

    lines = Path(api.__file__).read_text(encoding="utf-8").splitlines()

    # `/{memoir_id}/chat` is a POST that is deliberately read-only: it returns
    # an LLM reply and writes nothing. It is exempt, and the exemption is named
    # here rather than implemented as a magic count, so that when someone adds a
    # genuinely-writing POST they have to think about this.
    # Keyed by handler name rather than path. The decorator is multi-line in this
    # file, so the path is not on the same line as the verb and matching on it
    # silently exempts nothing.
    READ_ONLY_HANDLERS = {"chat_with_archive"}

    offenders: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("@organization_router."):
            continue
        if not any(f".{verb}(" in stripped for verb in ("post", "put", "patch", "delete")):
            continue

        handler = next(
            (j for j in range(index, min(index + 12, len(lines))) if "async def " in lines[j]),
            None,
        )
        if handler is None:
            continue

        name = lines[handler].split("async def ", 1)[1].split("(", 1)[0]
        if name in READ_ONLY_HANDLERS:
            continue

        body = lines[handler : handler + 45]
        if not any("assert_memoir_editable" in b for b in body):
            offenders.append(name)

    return offenders


def test_every_write_route_checks_editability():
    """Every memoir-content write path, not just apply."""
    offenders = _write_routes_without_an_editability_check()
    assert not offenders, (
        "write routes with no assert_memoir_editable check, so an owner editing "
        "a published memoir gets a 500 from the trigger instead of a 409: "
        f"{offenders}"
    )


def test_the_chat_route_writes_nothing():
    """
    The exemption above is only sound if chat really is read-only.

    `chat_with_archive` returns `(reply, proposed_actions)` and the actions are
    proposals the owner must confirm through a separate route. If it ever started
    writing, the exemption would be a silent hole in the immutability invariant
    -- and it would be the worst possible place for one, because AI-authored
    writes are the ones nobody is watching.
    """
    import inspect

    from src.domain import organization_service as svc

    source = inspect.getsource(svc.chat_with_archive)

    for forbidden in (
        "_apply_resolved_plan",
        "insert_chapters",
        "assign_memories_to_chapters",
        "update(",
        "delete(",
    ):
        assert forbidden not in source, (
            f"chat_with_archive now performs {forbidden!r}; it is exempted from "
            "assert_memoir_editable on the basis that it writes nothing"
        )


@pytest.mark.live
async def test_the_database_trigger_blocks_writes_to_a_published_memoir():
    """
    The invariant that actually holds, asserted against the live schema.

    Root AGENTS.md invariant 4: enforced twice -- application code and a Postgres
    trigger -- because "if you add a write path you must call the first" is a rule
    a codebase eventually forgets. This is the second half, and it is the half
    that matters: a future route, admin script, console session or migration that
    writes `memory.chapter_id` on a published memoir hits this.

    Marked `live` because it needs a real database. `-m "not live"` runs
    everything else.
    """
    with live_connection() as conn:
        rows = conn.execute(
            """
            select c.relname
            from pg_trigger t
            join pg_class c on c.oid = t.tgrelid
            join pg_proc p on p.oid = t.tgfoid
            where p.proname = 'prevent_writes_to_published_memoir'
              and c.relname in ('memory', 'media_asset', 'transcript')
            """
        ).fetchall()

    guarded = {r[0] for r in rows}
    assert "memory" in guarded, (
        "no immutability trigger on `memory`; the organization feature writes "
        "memory.chapter_id, so a published memoir could be reorganized"
    )


@pytest.mark.live
async def test_a_published_memoir_really_rejects_a_chapter_assignment():
    """
    Proves the trigger FIRES, not merely that it exists.

    A trigger can exist and be disabled, fire only on DELETE, or reference a
    function whose body was edited into a no-op. `pg_trigger` proves none of that;
    only an actual blocked write does.

    A savepoint wraps the expected failure. Without one, the trigger's error
    aborts the whole transaction and the scaffolding disappears with it -- which
    would make the assertion pass for the wrong reason on a run where the trigger
    was absent but the scaffolding insert also failed.
    """
    with live_connection() as conn:
        # Order matters, and getting it wrong produces a baffling failure. The
        # trigger fires on INSERT to `memory` when the parent memoir is
        # published, so scaffolding a *published* memoir first makes the setup
        # itself raise the very error under test -- which reads as a broken test
        # rather than as a trigger that works.
        #
        # So: build as a draft, publish, then probe.
        scaffold_memoir(conn, status="draft")
        scaffold_submitted_memory(conn)
        conn.execute(
            "update memoir set status = 'published', published_at = now() where id = %s",
            (SANDBOX_MEMOIR_ID,),
        )

        conn.execute("savepoint before_trigger_probe")

        try:
            conn.execute(
                "update memory set chapter_id = %s where id = %s",
                ("66666666-6666-6666-6666-666666666666", SANDBOX_MEMORY_ID),
            )
        except Exception as exc:
            # Matched by content, not by type: a dropped connection or a missing
            # scaffolding row would otherwise pass as "the trigger worked".
            assert "published" in str(exc).lower(), (
                f"expected the published-memoir error, got {type(exc).__name__}: {exc}"
            )
            conn.execute("rollback to savepoint before_trigger_probe")
        else:
            raise AssertionError(
                "the immutability trigger did NOT block a chapter assignment on a "
                "published memoir; invariant 4 is unenforced"
            )

        conn.execute("rollback")


@pytest.mark.live
async def test_the_one_live_proposal_per_memoir_index_is_enforced_in_postgres():
    """
    Two concurrent organize runs both supersede then insert. Application-level
    ordering does not help across two HTTP round-trips, so the invariant is a
    partial unique index. Asserted by violating it, not by reading its definition.

    The negative case matters as much: a second *applied* proposal must be
    allowed, or the audit history stops accumulating after one run.
    """
    with live_connection() as conn:
        scaffold_memoir(conn)

        insert = """
            insert into organization_proposal (memoir_id, status, payload, source)
            values (%s, %s, '{}'::jsonb, 'organize')
        """
        conn.execute(insert, (SANDBOX_MEMOIR_ID, "pending"))

        conn.execute("savepoint before_index_probe")
        with pytest.raises(Exception) as exc:
            conn.execute(insert, (SANDBOX_MEMOIR_ID, "pending"))
        assert "uq_organization_proposal_one_pending" in str(exc), (
            "a second pending proposal was accepted; the constraint that makes "
            f"concurrent organize runs safe is not in place: {exc}"
        )
        conn.execute("rollback to savepoint before_index_probe")

        # History must still accumulate.
        conn.execute(insert, (SANDBOX_MEMOIR_ID, "applied"))
        conn.execute(insert, (SANDBOX_MEMOIR_ID, "applied"))

        conn.execute("rollback")


@pytest.mark.live
async def test_an_id_is_generated_by_the_database_not_the_orm():
    """
    Application code writes through PostgREST, not the ORM, so a Python-side
    `default=uuid.uuid4` never runs. Every insert omitting `id` would fail on
    NOT NULL, and the first such insert is a user clicking a button.

    Asserted as the insert the service actually performs.
    """
    with live_connection() as conn:
        scaffold_memoir(conn)

        generated = conn.execute(
            """
            insert into organization_job_run (memoir_id, status)
            values (%s, 'running') returning id
            """,
            (SANDBOX_MEMOIR_ID,),
        ).fetchone()

        assert generated[0] is not None, "the server did not generate an id"

        conn.execute("rollback")


@pytest.mark.live
async def test_the_organization_status_columns_are_intact():
    """
    The columns that `cd3760273110` drops and re-adds.

    That migration is the reason `scripts/migrate.py` refuses to advance from an
    unexpected revision: re-running it erases these. This test does not prevent
    that -- the guarded runner does -- but it names what would be lost, so the
    runbook's warning is about something concrete.

    `organization_error_message` is a TEXT column carrying up to a paragraph of
    provider diagnostics, so it is the one with real content to lose.
    """
    with live_connection() as conn:
        rows = conn.execute(
            """
            select column_name from information_schema.columns
            where table_schema = 'public' and table_name = 'memoir'
              and column_name like 'organization%'
            """
        ).fetchall()

    present = {r[0] for r in rows}
    for required in (
        "organization_status",
        "organization_started_at",
        "organization_completed_at",
        "organization_error_message",
    ):
        assert required in present, (
            f"memoir.{required} is missing. If cd3760273110 was re-run, it was "
            "dropped and re-added empty: every organize run's outcome is gone."
        )
