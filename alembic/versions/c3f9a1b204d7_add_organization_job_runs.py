"""add per-stage progress tracking for the AI organization pipeline

Revision ID: c3f9a1b204d7
Revises: d8ebd28b0d20
Create Date: 2026-10-03 10:15:00.000000

Two tables: `organization_job_run` (one row per organize run) and
`organization_agent_run` (one row per pipeline stage within that run).

WHY

The memoir row carries organization_status / _started_at / _completed_at,
which answers "did it finish?" but not "where is it now?". The pipeline is
Reader -> Organizer -> Resolver, and three things need to be knowable while it
runs:

  1. Which stage is executing, so an owner sees progress instead of a spinner.
  2. Which provider actually served each stage. The LLM layer fails over
     between Groq and Gemini on rate limits and 5xx; without a per-stage record
     that failover is invisible after the fact and a chronic misconfiguration
     looks identical to a healthy run.
  3. How far a dead job got. BackgroundTasks has no heartbeat and no crash
     recovery, so a process killed mid-run leaves organization_status at
     'running' forever. `compute_effective_organization_status` synthesizes
     'stalled' from elapsed time, but only a per-stage row tells you whether it
     died reading 200 memories or on the third provider retry.

DESIGN NOTES

Status columns are `text` + CHECK rather than Postgres enums. An enum needs
`ALTER TYPE ... ADD VALUE` to extend, which cannot run inside a transaction on
older Postgres and turns a routine status addition into a migration review. A
check constraint is extended the same way but stays local to this table and
cannot be silently widened by an unrelated feature that shares a vocabulary with
it. `agent_role` is text by the same reasoning -- the agent set is expected to
grow, and a new stage should be a new row rather than a migration.

No new `memoir.organization_status` values are added. `stalled` is derived at
read time, because storing it would persist a claim about elapsed time that
stops being true the moment it is written.

Both tables cascade on memoir delete: a run is meaningless without its memoir,
and an orphaned run row is indistinguishable from a job for a memoir that still
exists.

This supersedes the hand-written migrations/0006_organization_job_runs.sql,
which was never applied. That file has been deleted rather than left in place:
two files describing the same schema, one of them dead, is how the next person
ends up not knowing which one runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c3f9a1b204d7"
down_revision: Union[str, None] = "d8ebd28b0d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_job_run",
        # The server default on `id` is load-bearing, not decoration. All writes
        # to these tables go through PostgREST (the Supabase REST client), where
        # a Python-side default never runs -- so without `gen_random_uuid()`
        # here, every insert that omits `id` fails on NOT NULL.
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("memoir_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("provider_used", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running', 'ready', 'failed')",
            name="ck_organization_job_run_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_organization_job_run_attempt"),
        sa.CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_organization_job_run_time_order",
        ),
        # PK and FK are left unnamed so Postgres assigns its defaults, matching
        # what SQLAlchemy emits from the model and matching every table already
        # in this schema. Naming them here but not in the model would make the
        # next `--autogenerate` propose renaming constraints that do not need
        # renaming.
        sa.ForeignKeyConstraint(["memoir_id"], ["memoir.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_organization_job_run_memoir",
        "organization_job_run",
        ["memoir_id", "started_at"],
        unique=False,
    )

    op.create_table(
        "organization_agent_run",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("job_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("provider_used", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running', 'ready', 'failed')",
            name="ck_organization_agent_run_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_organization_agent_run_attempt"),
        sa.CheckConstraint(
            "duration_ms is null or duration_ms >= 0",
            name="ck_organization_agent_run_duration",
        ),
        sa.CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_organization_agent_run_time_order",
        ),
        sa.ForeignKeyConstraint(["job_run_id"], ["organization_job_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_organization_agent_run_job",
        "organization_agent_run",
        ["job_run_id", "agent_role"],
        unique=False,
    )

    # RLS enabled with no policies, matching every other table in this schema.
    # The application reads and writes these through the service-role client,
    # which bypasses RLS, after it has verified ownership itself. Enabled rather
    # than omitted so that if anyone ever queries with the anon key -- or a
    # future direct Supabase client is introduced -- they get no rows rather
    # than every family's organize history.
    op.execute("alter table public.organization_job_run enable row level security")
    op.execute("alter table public.organization_agent_run enable row level security")

    op.execute(
        "comment on table public.organization_job_run is "
        "'One row per AI organization run. Created by POST /api/memoirs/{id}/organize.'"
    )
    op.execute(
        "comment on table public.organization_agent_run is "
        "'One row per pipeline stage (reader/organizer/refiner/resolver) within a job run.'"
    )
    op.execute(
        "comment on column public.organization_agent_run.agent_role is "
        "'Pipeline stage name. Text not enum so new stages do not require a migration.'"
    )
    op.execute(
        "comment on column public.organization_job_run.error_message is "
        "'Owner-facing text. Never memory content and never a provider payload.'"
    )


def downgrade() -> None:
    op.drop_index("idx_organization_agent_run_job", table_name="organization_agent_run")
    op.drop_table("organization_agent_run")

    op.drop_index("idx_organization_job_run_memoir", table_name="organization_job_run")
    op.drop_table("organization_job_run")