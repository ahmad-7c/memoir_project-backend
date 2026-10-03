#!/usr/bin/env python
"""
@file backend/scripts/migrate.py
@description Guarded Alembic runner. Refuses to migrate from a revision whose
predecessors are not safe to re-run.

WHY THIS EXISTS INSTEAD OF `alembic upgrade head` IN AN ENTRYPOINT

Three revisions in the chain are not idempotent:

    879e2c8d1b8c  add memory_media foreign keys    -- ADD CONSTRAINT
    080151e0f1e8  (drop constraint)                 -- DROP CONSTRAINT
    cd3760273110  organization job status            -- DROP COLUMN + ADD COLUMN

If the database's `alembic_version` is at or before any of those, a blind
`upgrade head` either fails on a duplicate object or — worse — drops
`organization_status` and its siblings and re-adds them empty, silently erasing
every organize run the memoir has ever had.

Put that in a container entrypoint and it stops being a configuration mistake
and becomes data loss with no rollback, triggered by the deploy that was meant
to fix something else.

So this runner reads `alembic_version` first and advances only from a revision
whose remaining migrations are pure `CREATE TABLE`. Everything else stops the
deploy with an explanation.

    python scripts/migrate.py            # guarded migrate
    python scripts/migrate.py --check    # report only, exit 1 if not at head
    python scripts/migrate.py --force    # operator override, logs loudly

`--force` exists because refusing is only correct while the assumption holds.
An operator who has verified a specific database is safe should not have to edit
this file to unblock a deploy.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from src.core.config import settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("migrate")

# What is safe to advance FROM, and why.
#
# The set of revisions AHEAD of each entry is what matters. Advance one revision
# at a time and read the chain:
#
#   cd3760273110 --[safe]--> d8ebd28b0d20 --[safe]--> c3f9a1b204d7 --> e7a41c5b9f32
#         ^
#    SAFE_TO_ADVANCE_FROM
#
# `d8ebd28b0d20` is a single `CREATE OR REPLACE FUNCTION` -- idempotent by
# construction, and it deliberately does not recreate triggers, so the existing
# ones keep pointing at the replaced function.
#
# `c3f9a1b204d7` and `e7a41c5b9f32` are pure CREATE TABLE / CREATE INDEX on
# tables that do not yet exist. Nothing ahead of this set drops, alters or
# re-adds a column, so no row is read, written or destroyed.
#
# Anything EARLIER is refused. Reaching it would re-run `879e2c8d1b8c`
# (ADD CONSTRAINT) or `080151e0f1e8` (DROP CONSTRAINT), which fail on a
# duplicate/missing object, or -- if the database sits behind it rather than
# between revisions -- `cd3760273110`, which does DROP COLUMN + ADD COLUMN on
# memoir.organization_status and would erase every organize run this project has
# ever recorded. That failure is silent and has no rollback.
SAFE_TO_ADVANCE_FROM = {"cd3760273110", "d8ebd28b0d20"}

# Nothing behind HEAD may be re-run, but HEAD itself needs no work.
HEAD = "e7a41c5b9f32"


def read_current_revision(engine) -> str | None:
    """
    Returns the single revision stamped on the database.

    Reads the table directly rather than through `alembic current`: this has to
    work even when the chain itself is what is in question, and it has to work
    when the table does not exist yet, which `alembic` reports as an error
    rather than as "unmigrated".
    """
    with engine.connect() as conn:
        exists = conn.execute(
            text(
                "select exists (select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = 'alembic_version')"
            )
        ).scalar()
        if not exists:
            return None

        rows = conn.execute(text("select version_num from alembic_version")).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # Multiple heads means two migration paths were applied to one database.
        # Picking either one is a guess, so refuse.
        raise RuntimeError(
            f"database has {len(rows)} alembic revisions stamped "
            f"({', '.join(str(r[0]) for r in rows)}); it has forked and must be "
            "reconciled by hand before any automated migration"
        )
    return str(rows[0][0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report the current revision and exit 1 if it is not HEAD. No writes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run alembic upgrade head regardless of the starting revision.",
    )
    args = parser.parse_args()

    # Fail fast and loudly if the process was started without secrets. Letting
    # Alembic fail instead produces a traceback about a connection string.
    if not settings.database_url:
        logger.error("DATABASE_URL is empty; refusing to attempt a migration.")
        return 2

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )

    try:
        current = read_current_revision(engine)
    except Exception:
        logger.exception("could not read alembic_version")
        return 2

    logger.info("database revision: %s", current or "<none>")

    if current == HEAD:
        logger.info("already at head (%s); nothing to do.", HEAD)
        return 0

    if args.check:
        logger.error("not at head: %s (expected %s)", current or "<none>", HEAD)
        return 1

    if current is None:
        logger.error(
            "No alembic_version table, so this database was never migrated by "
            "Alembic. It was most likely created from migrations/0000_bootstrap_sandbox_schema.sql, "
            "which is the intended path for an existing project -- stamp it first with:\n"
            "    alembic stamp d8ebd28b0d20\n"
            "and confirm that revision describes this database before doing so. "
            "Refusing to guess."
        )
        return 2

    if current not in SAFE_TO_ADVANCE_FROM and not args.force:
        logger.error(
            "Refusing to migrate from %s.\n"
            "The revisions between %s and head are not idempotent: cd3760273110 "
            "drops and re-adds the organization_status columns, and 879e2c8d1b8c / "
            "080151e0f1e8 add and drop constraints by name. Running them a second "
            "time either fails or erases organize history.\n"
            "Inspect what this database actually has before forcing it:\n"
            "    SELECT column_name FROM information_schema.columns\n"
            "      WHERE table_name = 'memoir' AND column_name LIKE 'organization%%';\n"
            "Once you have confirmed the database is safe to advance, re-run with --force.",
            current,
            current,
        )
        return 2

    if args.force and current not in SAFE_TO_ADVANCE_FROM:
        logger.warning("--force: advancing from %s, which was not on the safe list.", current)

    logger.info("running: alembic upgrade head")
    os.chdir(BACKEND_ROOT)
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_cfg, "head")

    engine.dispose()
    logger.info("migration complete; database is at %s.", HEAD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
