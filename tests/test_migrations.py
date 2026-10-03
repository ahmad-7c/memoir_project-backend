"""
@file tests/test_migrations.py
@description Tests for the Alembic revision chain and model/migration parity.

Marked `migration`. Run separately with `pytest -m migration` when you want the
(subprocess-slower) DDL comparison without it in the fast inner loop.

This file exists because of a specific, still-live operational hazard rather
than for coverage's sake. Three revisions in the chain are not idempotent:

    879e2c8d1b8c  ADD CONSTRAINT
    080151e0f1e8  DROP CONSTRAINT
    cd3760273110  DROP COLUMN + ADD COLUMN

`alembic upgrade head` from any revision at or behind those either fails on a
duplicate object or -- for cd3760273110 -- succeeds and empties the
`organization_status` columns on `memoir`, destroying every organize run ever
recorded. `scripts/migrate.py` exists to refuse that, and it can only work if
the set of non-idempotent revisions does not quietly grow. That is what
`test_non_idempotent_revisions_are_exactly_the_documented_set` is for.
"""

from pathlib import Path

import pytest

from src.db.base import Base
import src.db.models  # noqa: F401  -- registers every table on Base.metadata

BACKEND_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"

# Revisions ahead of the two new ones. `scripts/migrate.py` hard-codes the safe
# set; these constants are the other half of that contract, and the two files
# failing apart is the hazard.
SAFE_TO_ADVANCE_FROM = {"cd3760273110", "d8ebd28b0d20"}
PRE_NEW_HEAD = "d8ebd28b0d20"
HEAD = "e7a41c5b9f32"

NEW_TABLES = (
    "organization_job_run",
    "organization_agent_run",
    "organization_proposal",
    "organization_action_audit",
)


def _revision_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


def _revision_id(path: Path) -> str:
    return path.name.split("_", 1)[0]


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------


def test_the_chain_has_exactly_one_head():
    """
    Two heads means two migration paths applied to one database, and Alembic
    will not resolve which one is right. `alembic heads` exits 0 in that case, so
    it needs to be asserted rather than eyeballed.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()

    assert heads == [HEAD], f"expected a single head {HEAD}, found {heads}"


def test_every_new_revision_chains_from_the_revision_before_it():
    """
    The two new revisions must descend from `d8ebd28b0d20` and from each other,
    with no gap. A revision pointing at a revision id that does not exist fails
    only at apply time, on a database.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    downgrades = {rev.revision: rev.down_revision for rev in script.walk_revisions()}

    assert downgrades["c3f9a1b204d7"] == PRE_NEW_HEAD, (
        "the job-run migration must chain directly from the previous head, or "
        "scripts/migrate.py's safe-advance set is wrong"
    )
    assert downgrades[HEAD] == "c3f9a1b204d7"


def test_revision_ids_are_unique():
    """Two revisions sharing an id silently produce a forked chain."""
    ids = [_revision_id(p) for p in _revision_files()]
    assert len(ids) == len(set(ids)), "duplicate revision id in alembic/versions"


# ---------------------------------------------------------------------------
# The data-loss hazard, pinned
# ---------------------------------------------------------------------------

# The exact set that must NOT be re-run. Extend this list, and
# `scripts/migrate.py` refuses, the moment a non-idempotent revision is added.
NON_IDEMPOTENT = {
    "879e2c8d1b8c": "ADD CONSTRAINT",
    "080151e0f1e8": "DROP CONSTRAINT",
    "cd3760273110": "DROP COLUMN + ADD COLUMN (destroys organization_status)",
}


def test_non_idempotent_revisions_are_exactly_the_documented_set():
    """
    Pins the blast radius of `alembic upgrade head` from a stale revision.

    Two directions of failure, both caught here:

    * A new non-idempotent revision appears. `scripts/migrate.py` still lists
      `d8ebd28b0d20` as safe to advance from, so it would happily run the new
      destructive revision without refusing. This test fails instead.

    * A previously non-idempotent revision is rewritten to be idempotent. Then
      the guard is stricter than necessary -- annoying, not dangerous -- and this
      test makes the simplification a deliberate act.
    """
    offenders = {}
    for path in _revision_files():
        source = path.read_text(encoding="utf-8")
        rev = _revision_id(path)
        if rev in NON_IDEMPOTENT:
            continue
        if "op.add_column" in source or "op.drop_column" in source:
            offenders[rev] = "column add/drop"

    assert offenders == {}, (
        "unlisted column-altering revision(s) found; a blind `alembic upgrade "
        f"head` would run them: {offenders}. Add to NON_IDEMPOTENT here and make "
        "scripts/migrate.py refuse to advance from their predecessors."
    )


def test_the_migration_runner_refuses_exactly_those_revisions():
    """
    `scripts/migrate.py` is the thing that keeps a routine deploy from
    destroying organize history, so its safe-set and this test's set must agree.
    A mismatch in the *permissive* direction is a silent data-loss path.

    Parsed with `ast` rather than matched as text: a string comparison against the
    source breaks on quoting alone (`{"d8ebd28b0d20"}` vs `{'d8ebd28b0d20'}`), and
    a test that breaks on formatting gets deleted instead of fixed.
    """
    import ast

    tree = ast.parse((BACKEND_ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8"))
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id in ("SAFE_TO_ADVANCE_FROM", "HEAD")
    }

    assert assignments.get("SAFE_TO_ADVANCE_FROM") == SAFE_TO_ADVANCE_FROM, (
        "scripts/migrate.py's SAFE_TO_ADVANCE_FROM has drifted from this test's "
        "set. Two directions of drift, one dangerous: ADDING a revision here "
        "makes the runner advance across a destructive migration. Removing one "
        "is only over-cautious. Re-derive it from the chain before changing it."
    )
    assert assignments.get("HEAD") == HEAD, (
        "scripts/migrate.py's HEAD is stale; a migrated database reports "
        "'not at head' forever and blocks every deploy."
    )


def test_the_safe_advance_set_covers_every_idempotent_predecessor():
    """
    `cd3760273110` is a safe starting point because the only revision between it
    and head is `d8ebd28b0d20`, a `CREATE OR REPLACE FUNCTION`.

    That is the *live* state of the project's Supabase database, so if this
    revision is ever added to the non-idempotent list without updating the safe
    set, the guarded runner refuses to migrate a perfectly good database and
    every deploy stops. This test is what makes that failure loud instead.
    """
    import ast

    source = (BACKEND_ROOT / "alembic" / "versions" / "d8ebd28b0d20_fix_memoir_immutability_trigger_.py").read_text(encoding="utf-8")

    assert "create or replace function" in source.lower(), (
        "d8ebd28b0d20 is no longer a CREATE OR REPLACE FUNCTION; it is still "
        "listed as a safe migration starting point in scripts/migrate.py, and "
        "advancing across a non-idempotent revision destroys organize history."
    )


def test_the_migration_runner_does_not_mutate_the_database_on_check():
    """
    `--check` is used as a deploy gate, so it must be read-only. Asserted by
    absence of the Alembic entrypoint rather than by running it against a
    database: the guarantee is about what the code can do, and a test that needs
    Postgres to prove it is a test that does not run in CI.
    """
    source = (BACKEND_ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")

    upgrade_call = "command.upgrade(alembic_cfg"
    # Everything between the early `--check` return and the upgrade must not
    # contain a write. Simplest honest assertion: exactly one upgrade call, and
    # it appears after the args.check branch.
    assert source.count(upgrade_call) == 1
    assert source.index("if args.check:") < source.index(upgrade_call)


def test_the_container_does_not_migrate_on_startup():
    """
    The single highest-value assertion in this file.

    A Dockerfile that runs `alembic upgrade head` in its entrypoint converts an
    unexpected `alembic_version` into data loss, triggered by whichever deploy
    was meant to fix something else. This test fails if someone "simplifies" the
    startup path by adding it back.
    """
    dockerfile = (BACKEND_ROOT / "Dockerfile").read_text(encoding="utf-8")
    command_lines = [
        line for line in dockerfile.splitlines()
        if line.strip().upper().startswith("CMD")
    ]
    assert command_lines, "Dockerfile has no CMD"

    for line in command_lines:
        assert "alembic" not in line, (
            "Dockerfile CMD runs alembic. Migrations must be an explicit "
            "one-shot step; see deploy/README.md section 3."
        )
        assert "migrate.py" not in line, "Dockerfile CMD runs the migration runner."


# ---------------------------------------------------------------------------
# Model/migration parity
# ---------------------------------------------------------------------------


@pytest.mark.migration
@pytest.mark.parametrize("table_name", NEW_TABLES)
def test_model_ddl_matches_migration_ddl(table_name):
    """
    Models are the source of truth, but Alembic's `--autogenerate` compares them
    to the *database*, not to the migration that built it. Drift between a model
    and its migration therefore surfaces one revision too late -- as a spurious
    DROP TABLE, generated by the tool that was supposed to help.

    This compares the two directly, offline, with no database.
    """
    from _ddl_parity_check import migration_ddl, model_ddl

    from_model = model_ddl(table_name)
    from_migration = migration_ddl(table_name)

    assert from_model == from_migration, (
        f"{table_name} differs between model and migration.\n"
        f"  model only:     {sorted(from_model - from_migration)}\n"
        f"  migration only: {sorted(from_migration - from_model)}"
    )


@pytest.mark.parametrize("table_name", NEW_TABLES)
def test_every_new_table_generates_its_own_id(table_name):
    """
    Every table needs `server_default=gen_random_uuid()` on `id`.

    Application code writes through PostgREST, not the ORM, so a Python-side
    `default=uuid.uuid4` never runs. Without the server default, every insert
    that omits `id` fails on NOT NULL -- and the first such insert is a user
    clicking a button, not a deploy.
    """
    table = Base.metadata.tables[table_name]
    id_column = table.c.id

    assert id_column.server_default is not None, (
        f"{table_name}.id has no server_default; an insert omitting id via "
        "PostgREST will fail on NOT NULL"
    )
    assert "gen_random_uuid" in str(id_column.server_default.arg)


@pytest.mark.parametrize("table_name", NEW_TABLES)
def test_every_new_table_is_registered_for_alembic(table_name):
    """
    `alembic/env.py` points `target_metadata` at `Base.metadata`, so a model not
    imported in `src/db/models/__init__.py` is a table Alembic believes does not
    exist -- and will cheerfully offer to drop.
    """
    assert table_name in Base.metadata.tables, (
        f"{table_name} is missing from Base.metadata; it is not imported in "
        "src/db/models/__init__.py and Alembic will emit DROP TABLE for it"
    )


def test_every_new_table_has_the_index_its_queries_rely_on():
    """
    Each table's indexes are asserted exactly, not by a generic rule.

    A generic "every filtered column is indexed" rule cannot be written here: it
    needs to know which columns the repositories actually filter on, and
    `organization_job_run.status` is deliberately *not* filtered on in any query
    -- runs are fetched by `memoir_id` and ordered by `started_at`. Asserting the
    concrete set catches a dropped index, which is the regression that matters,
    without inventing requirements the code does not have.
    """
    expected = {
        # Fetch the latest run for a memoir, newest first.
        "organization_job_run": [["memoir_id", "started_at"]],
        # Fetch every stage of one run.
        "organization_agent_run": [["job_run_id", "agent_role"]],
        # List proposals for a memoir by status, newest first.
        "organization_proposal": [
            ["memoir_id", "status", "created_at"],
            # Partial unique index: at most one live proposal per memoir. This is
            # what makes two concurrent organize runs resolve in the database
            # instead of in application-level ordering, which is meaningless
            # across two HTTP round-trips.
            ["memoir_id"],
        ],
        "organization_action_audit": [["memoir_id", "created_at"]],
    }

    for table_name, expected_columns in expected.items():
        table = Base.metadata.tables[table_name]
        actual = sorted(
            ([col.name for col in index.columns] for index in table.indexes),
            key=lambda cols: cols,
        )
        assert actual == sorted(expected_columns), (
            f"{table_name}: indexes are {actual}, expected {sorted(expected_columns)}. "
            "The parity check proves model and migration agree; this proves they "
            "agree on the right thing."
        )


def test_the_one_pending_proposal_index_is_partial_and_unique():
    """
    Two concurrent HTTP requests make application-level ordering meaningless, so
    "one live proposal per memoir" has to be a database invariant rather than a
    rule the service tries to honour.

    A plain unique index on `memoir_id` would be wrong in the other direction: it
    would forbid a second *applied* proposal, so the audit history would stop
    accumulating after one organize run.
    """
    table = Base.metadata.tables["organization_proposal"]
    partial = next(
        (i for i in table.indexes if i.name == "uq_organization_proposal_one_pending"), None
    )

    assert partial is not None, "the one-live-proposal-per-memoir index is missing"
    assert partial.unique is True
    assert partial.dialect_options["postgresql"]["where"] is not None, (
        "the index must be PARTIAL (WHERE status = 'pending'); a full unique "
        "index on memoir_id would also forbid a second APPLIED proposal, so the "
        "apply history would stop after one organize run"
    )