"""
@file backend/_ddl_parity_check.py
@description Asserts that the SQLAlchemy models and the Alembic migrations agree.

WHY THIS EXISTS AS A SCRIPT RATHER THAN A TEST

`alembic revision --autogenerate` compares `Base.metadata` against the live
database. It has no opinion about whether the *migration that created a table*
matches the *model that describes it*. If those two drift, the symptom appears
one migration too late: someone runs `--autogenerate`, Alembic helpfully emits a
`DROP TABLE` for a table that is very much in use, and nobody notices until it
is applied.

Two specific drifts were found and fixed while writing these tables:

  - `id` had a Python-side `default=uuid.uuid4` but no `server_default`. Every
    write to these tables goes through PostgREST, where the Python default never
    runs, so an insert omitting `id` would fail on NOT NULL.
  - A composite index was declared `created_at DESC`, which forced it to become
    an expression index. Ascending is equivalent for the query it serves,
    because a btree scans in both directions.

This script compiles the models to DDL and compares it against the DDL Alembic
actually emits in offline (`--sql`) mode. Run it after touching any model or
migration in src/db/models/ or alembic/versions/.

    python _ddl_parity_check.py

Exits 0 when the two agree, 1 otherwise. Requires no database connection.
"""

import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.db.base import Base
import src.db.models  # noqa: F401  -- registers every table on Base.metadata

BACKEND_ROOT = Path(__file__).resolve().parent
NEW_TABLES = (
    "organization_job_run",
    "organization_agent_run",
    "organization_proposal",
    "organization_action_audit",
)


def _squash(sql: str) -> str:
    """Collapse all whitespace and drop statement terminators."""
    return re.sub(r"\s+", " ", sql.replace("public.", "")).replace(" ;", ";").strip().rstrip(";")


def _split_top_level(body: str) -> list[str]:
    """
    Splits a CREATE TABLE body on commas that are not inside parentheses.

    `payload JSONB NOT NULL` has no commas, but a CHECK constraint does
    ('running', 'ready', 'failed'), so splitting on every comma would tear those
    apart and make the comparison meaningless.
    """
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [_squash(p) for p in parts if _squash(p)]


def _table_defs(sql: str, table_name: str) -> set[str]:
    """Column and constraint definitions for one CREATE TABLE."""
    # The trailing `;` is optional: SQLAlchemy's compiler does not emit one,
    # Alembic's offline writer does.
    match = re.search(
        rf"CREATE TABLE {table_name} \((.*?)\n\)\s*;?", sql, re.DOTALL | re.IGNORECASE
    )
    if not match:
        raise AssertionError(f"no CREATE TABLE for {table_name}")
    return set(_split_top_level(match.group(1)))


def _index_defs(sql: str, table_name: str) -> set[str]:
    """Every CREATE INDEX that targets this table."""
    found = set()
    # Single-line match, and the terminator is optional because SQLAlchemy's
    # CreateIndex compiler does not emit a semicolon while Alembic's writer does.
    for match in re.finditer(r"CREATE (UNIQUE )?INDEX [^\n;]*", sql):
        text = _squash(match.group(0))
        if re.search(rf"ON {table_name}\b", text):
            found.add(text)
    return found


def model_ddl(table_name: str) -> set[str]:
    table = Base.metadata.tables[table_name]
    dialect = postgresql.dialect()
    sql = "\n".join(
        [str(CreateTable(table).compile(dialect=dialect))]
        + [
            str(CreateIndex(ix).compile(dialect=dialect))
            for ix in sorted(table.indexes, key=lambda i: i.name or "")
        ]
    )
    return _table_defs(sql, table_name) | _index_defs(sql, table_name)


def migration_ddl(table_name: str) -> set[str]:
    """Runs Alembic in offline mode and returns this table's DDL definitions."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "d8ebd28b0d20:head", "--sql"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic --sql failed:\n{result.stderr}")
    return _table_defs(result.stdout, table_name) | _index_defs(result.stdout, table_name)


def main() -> int:
    failures = []
    for table_name in NEW_TABLES:
        from_model = model_ddl(table_name)
        from_migration = migration_ddl(table_name)
        if from_model == from_migration:
            print(f"  OK    {table_name}  ({len(from_model)} definitions)")
            continue

        failures.append(table_name)
        print(f"  DIFF  {table_name}")
        for part in sorted(from_model - from_migration):
            print(f"          model only:     {part}")
        for part in sorted(from_migration - from_model):
            print(f"          migration only: {part}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} table(s) differ: {', '.join(failures)}")
        print("A future --autogenerate will emit spurious DDL for these. Reconcile them.")
        return 1

    print(f"PASS: all {len(NEW_TABLES)} tables identical between models and migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())