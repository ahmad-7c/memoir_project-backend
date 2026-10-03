"""
@file src/db/models/organization_job_run.py
@description One row per AI organization run, plus per-stage progress rows.

WHY THIS EXISTS

The memoir row carries `organization_status` / `_started_at` / `_completed_at`,
which answers "did it finish?" but not "where is it now?". A multi-agent pipeline
has a Reader, an Organizer and a Resolver, and three things need to be knowable
while it runs:

  1. Which stage is executing, so the owner sees progress rather than a spinner.
  2. Which provider actually served each stage, so a failover is diagnosable
     after the fact instead of invisible.
  3. Whether the job died mid-flight. BackgroundTasks has no heartbeat and no
     crash recovery, so a process killed mid-run leaves `organization_status`
     at 'running' forever; `compute_effective_organization_status` papers over
     that with a stall threshold, but a *per-stage* row tells you how far it got.

DELIBERATE NON-DECISIONS

- `status` is `Text + CheckConstraint`, not a Postgres enum. An enum needs
  `ALTER TYPE ... ADD VALUE` to extend, which cannot run inside a transaction on
  older Postgres and turns a routine status addition into a migration review.
  A check constraint is extended the same way but is local to this table and
  cannot be silently widened by an unrelated feature that happens to share a
  vocabulary with it. See AGENTS.md §7: new status values are added as a
  superset.
- No new `memoir.organization_status` values. `stalled` is derived at read time
  because storing it would persist a claim about elapsed time that stops being
  true the moment it is written.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# Mirrors the values the pipeline can actually produce. Kept as module
# constants so the pipeline and the model cannot drift apart silently.
RUN_STATUSES = ("running", "ready", "failed")
AGENT_STATUSES = ("running", "ready", "failed")

# Stage names. `agent_role` is text rather than an enum by design: the agent set
# is expected to grow, and adding a stage should not require a schema migration.
# The pipeline is the only writer, and the status endpoint reads whatever rows
# exist rather than assuming a fixed list.
AGENT_ROLES = ("reader", "organizer", "refiner", "resolver")


class OrganizationJobRun(Base):
    """One AI organization run for one memoir."""

    __tablename__ = "organization_job_run"
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'ready', 'failed')",
            name="ck_organization_job_run_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_organization_job_run_attempt"),
        # A completed run cannot have completed before it started. Cheap
        # invariant that catches clock-skew and bad-write bugs at the source
        # rather than in a report three months later.
        CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_organization_job_run_time_order",
        ),
        # "the current run for this memoir", hit on every status poll.
        # Ascending `started_at`: a btree scans in either direction, so the
        # `ORDER BY started_at DESC LIMIT 1` this index serves needs no DESC.
        Index("idx_organization_job_run_memoir", "memoir_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        # The server default is what actually matters here.
        #
        # Application code reaches Postgres through the Supabase REST client,
        # not this ORM, so `default=uuid.uuid4` never runs -- a PostgREST insert
        # that omits `id` would hit a NOT NULL violation. The server default is
        # the only thing that makes an ORM-less insert work, which is why every
        # id in the live bootstrap schema carries one. Declared in the model as
        # well so the model and the migration agree.
        server_default=text("gen_random_uuid()"),
    )
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")

    # Which provider actually served the pipeline. Null while running, and null
    # when the chain was exhausted before any provider succeeded -- that
    # distinction is the whole diagnostic value of the column, so it is left
    # genuinely null rather than defaulted to a placeholder.
    provider_used: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # Owner-facing, provider-free text. Never memory content, never a provider
    # payload: this column is read directly by the status endpoint and shown to
    # a family.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # No ORM `relationship()` here, matching every other model in this package.
    # These classes exist as the schema source of truth that Alembic diffs
    # against the live database; application code reaches Postgres through the
    # Supabase REST client and never instantiates them. The FK plus
    # `ondelete="CASCADE"` expresses the relationship where it is actually
    # enforced -- in the database -- rather than in Python objects nobody builds.


class OrganizationAgentRun(Base):
    """
    One pipeline stage within a run.

    Per-stage rows are what make a multi-stage pipeline show real progress. A
    single `running` boolean cannot distinguish "reading 200 memories" from
    "hung on the third provider retry", and those need different responses from
    the owner (wait vs. retry).
    """

    __tablename__ = "organization_agent_run"
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'ready', 'failed')",
            name="ck_organization_agent_run_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_organization_agent_run_attempt"),
        CheckConstraint(
            "duration_ms is null or duration_ms >= 0",
            name="ck_organization_agent_run_duration",
        ),
        CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_organization_agent_run_time_order",
        ),
        # The status response's stages array.
        Index("idx_organization_agent_run_job", "job_run_id", "agent_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_job_run.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 'reader' | 'organizer' | 'refiner' | 'resolver'. Text, not an enum, so a
    # new stage is a new row rather than a migration.
    agent_role: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    provider_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # Best-effort token accounting. Nullable throughout: the fallback provider
    # does not reliably return usage in JSON mode, and a missing count must not
    # be reported as zero.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)