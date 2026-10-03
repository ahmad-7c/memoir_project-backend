"""
@file tests/helpers.py
@description Live-database helper for tests marked `live`.

SEPARATE FROM conftest.PY ON PURPOSE

`live_connection` is a helper, not a fixture. A fixture that connects would be
autouse-eligible, which is the most common way a test suite ends up requiring a
database to run at all. Here, only tests that explicitly `with live_connection()`
need one, and a contributor with no database access runs the rest of the suite
with `-m "not live"` and loses nothing.

WHY THESE ASSERT AGAINST THE REAL DATABASE AT ALL

The invariants they cover cannot be verified any other way. A trigger can exist
and be disabled. A unique index can exist and be partial on the wrong predicate. A
`server_default` can be declared and never fire because the column is NOT NULL.
Each of those reads correctly in `pg_catalog` and fails in production, and the
symptom in both cases is a user-facing error on a button press.

Every connection here is rolled back. These tests write to the same tables the
application does, on the same database the developer uses, so leaving a scaffold
row behind would corrupt someone's real memoir.

The database this points at is the development/sandbox Supabase project. Not
production. A `live` test that mutates rows is only safe because of the
rollback, and the rollback is the reason this file is short.
"""

from contextlib import contextmanager

# Sandbox memoir + user used by the live tests. Fixed ids so a crashed run's
# leftovers are recognisable and idempotent (`on conflict do nothing`) rather than
# accumulating as anonymous rows.
SANDBOX_USER_ID = "33333333-3333-3333-3333-333333333333"
SANDBOX_MEMOIR_ID = "44444444-4444-4444-4444-444444444444"
SANDBOX_MEMORY_ID = "55555555-5555-5555-5555-555555555555"
SANDBOX_EMAIL = "ci-trigger-test@example.invalid"

#: The participant table is `memoir_participant`, not `participant`. It is also
#: keyed by `memoir_id` and requires `display_name`, so a scaffold needs both
#: rows and both in the right order -- and guessing the table name fails with a
#: bare "relation does not exist", which says nothing about what was being tested.
SANDBOX_PARTICIPANT_ID = "77777777-7777-7777-7777-777777777777"


#: The placeholder `conftest.py` installs so `src.core.config` can be imported
#: without a real database. Anything equal to this is not a usable database, and
#: a `live` test against it would fail with a connection timeout instead of
#: skipping with an explanation.
PLACEHOLDER_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def _url_from_dotenv() -> str | None:
    """
    Reads DATABASE_URL out of `.env` directly, bypassing `os.environ`.

    Necessary rather than merely tidy. `conftest.py` sets a placeholder
    DATABASE_URL before importing `src.core.config`, because that module builds
    `Settings()` at import time and refuses to import without one. A plain
    `os.environ` read therefore returns the placeholder, and every `live` test
    would try to connect to 127.0.0.1 and time out -- which reads as "the tests
    are broken" rather than "no database is configured here".

    `.env` holds the developer's real connection string and is gitignored, so
    this reaches it without any credential appearing in the repository.
    """
    from pathlib import Path

    dotenv_path = Path(__file__).resolve().parent.parent / ".env"
    if not dotenv_path.exists():
        return None

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("DATABASE_URL="):
            return stripped.split("=", 1)[1].strip().strip("'\"") or None
    return None


def database_url() -> str | None:
    """
    The usable Postgres URL for `live` tests, normalised for psycopg, or None.

    Order matters: the real environment wins, then `.env`, and the conftest
    placeholder is treated as "not configured" rather than as a database to
    connect to.

    `DATABASE_URL` carries a SQLAlchemy dialect prefix (`postgresql+psycopg://`)
    because Alembic requires it; psycopg does not understand `+psycopg`.
    """
    import os

    raw = os.environ.get("DATABASE_URL")
    if not raw or raw == PLACEHOLDER_URL:
        raw = _url_from_dotenv()

    if not raw or raw == PLACEHOLDER_URL:
        return None

    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        raw = raw.replace(prefix, "postgresql://")
    return raw


@contextmanager
def live_connection():
    """
    Yields a connection whose work is always discarded.

    Three things are guaranteed, and each matters:

    * Rollback on exit, including on exception. No `live` test can leave a row
      behind, which is what makes it acceptable for these to write at all.
    * A bounded connect timeout. An unreachable database must fail the test in
      ten seconds, not hang a CI job until the runner's own limit.
    * `autocommit=False`, so the scaffolding and the assertion live in one
      transaction. An aborted transaction (which a trigger error causes) is
      already rolled back by Postgres; making that explicit is what keeps the
      negative test's scaffolding from surviving.
    """
    import psycopg

    url = database_url()
    if not url:
        raise RuntimeError(
            "live_connection() found no usable DATABASE_URL. Run with "
            "-m 'not live' where no database is available."
        )

    conn = psycopg.connect(url, connect_timeout=10, autocommit=False)
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def scaffold_memoir(conn, *, status: str = "draft", memoir_id: str = SANDBOX_MEMOIR_ID) -> str:
    """
    Creates a user and memoir for a live test, idempotently.

    `memoir.video_bytes_cap` is NOT NULL with no database default -- the platform
    default is applied at creation time in application code, so a raw insert must
    supply it. That is worth knowing: it is the kind of column that makes an
    ad-hoc `psql` insert fail in a confusing way.
    """
    conn.execute(
        """
        insert into user_account (id, email, full_name)
        values (%s, %s, 'CI Scaffold') on conflict (id) do nothing
        """,
        (SANDBOX_USER_ID, SANDBOX_EMAIL),
    )
    # user -> memoir -> memoir_participant, in that order. `memoir_participant`
    # references `memoir`, and `memory.author_participant_id` references the
    # participant, so any other sequence is a foreign-key violation.
    #
    # `display_name` is NOT NULL with no default on the participant.
    conn.execute(
        """
        insert into memoir (id, subject_name, created_by_user_id, video_bytes_cap, status)
        values (%s, 'CI SCAFFOLD', %s, %s, %s)
        on conflict (id) do nothing
        """,
        (memoir_id, SANDBOX_USER_ID, 5 * 1024**3, status),
    )
    conn.execute(
        """
        insert into memoir_participant (id, memoir_id, user_id, role, display_name)
        values (%s, %s, %s, 'owner', 'CI Scaffold') on conflict (id) do nothing
        """,
        (SANDBOX_PARTICIPANT_ID, memoir_id, SANDBOX_USER_ID),
    )
    return memoir_id


def scaffold_submitted_memory(conn, memoir_id: str = SANDBOX_MEMOIR_ID) -> str:
    """A submitted memory the trigger test can try to reassign."""
    # Note the schema: `memory` has `author_participant_id`, not
    # `created_by_user_id`, and no `kind` column (kind lives on `media_asset`).
    # `body_text` is nullable, so a test that wants to prove the trigger protects
    # it must supply real text.
    conn.execute(
        """
        insert into memory (
            id, memoir_id, author_participant_id, body_text, status, submitted_at
        )
        values (%s, %s, %s, 'the original words', 'submitted', now())
        on conflict (id) do nothing
        """,
        (SANDBOX_MEMORY_ID, memoir_id, SANDBOX_PARTICIPANT_ID),
    )
    return SANDBOX_MEMORY_ID