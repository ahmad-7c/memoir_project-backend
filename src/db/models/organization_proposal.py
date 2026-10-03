r"""
@file src/db/models/organization_proposal.py
@description Server-held organization proposals and the append-only action audit.

WHY A PROPOSAL TABLE AT ALL

The reference implementation's apply endpoint took the whole chapter structure as
a request body:

    class ChapterApplyPayload(BaseModel):
        chapters: List[Dict[str, Any]]

...and wrote it to the database with no ownership check and no `memoir_id` scope
on the per-memory update. Any authenticated user could re-point another family's
memories into their own memoir.

The fix is structural rather than a validation layer: invert who holds the plan.
The server persists a validated proposal; the client holds only its id. The
confirm request body is `{proposal_id: str}` and there is no field in it that
could influence what gets written -- which is exactly why there is nothing to
validate at that endpoint.

It also buys the audit trail the reference had no concept of. If a chapter ever
looks wrong, there is a record of which run produced it, which owner confirmed
it, and what the plan contained at the time.

THE `applying` STATUS

`confirm_proposal` must be safe against two owners (or one owner double-
clicking) confirming the same proposal concurrently. Without protection both
requests read `status = 'pending'`, both write the full chapter set, and the
memoir ends up with duplicate chapters and an audit trail showing one apply.

Claiming the row by a conditional update -- `UPDATE ... WHERE status = 'pending'`
returning rows -- is a compare-and-swap that only one caller can win. That needs
a third in-flight state to move to, hence `applying`:

    pending  --claim-->  applying  --success-->  applied
                          |  \--failure-->       superseded / expired
                          \------failure-------->  pending  (restored)

If the process dies while `applying`, the row is stranded. That is recoverable
rather than silent: `is_proposal_expired` treats an `applying` row past the TTL
as reaped, on the same reasoning as a `pending` row past the TTL -- the memoir
has almost certainly changed and the plan cannot be trusted.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPLYING = "applying"
PROPOSAL_STATUS_APPLIED = "applied"
PROPOSAL_STATUS_SUPERSEDED = "superseded"
PROPOSAL_STATUS_EXPIRED = "expired"

PROPOSAL_STATUSES = (
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_APPLYING,
    PROPOSAL_STATUS_APPLIED,
    PROPOSAL_STATUS_SUPERSEDED,
    PROPOSAL_STATUS_EXPIRED,
)

PROPOSAL_SOURCES = ("organize", "chat")

# Append-only trail. Only ever inserted to; there is deliberately no update or
# delete path in the repository.
ACTION_TYPES = (
    "apply_proposal",
    "rename_chapter",
    "edit_chapter",
    "reorder_chapters",
    "move_memory",
    "chat",
)


class OrganizationProposal(Base):
    """
    A validated plan awaiting (or having received) owner confirmation.

    `payload` is the serialized `ResolvedPlan`. jsonb rather than a normalised
    set of rows because the plan is written once, read once, and never queried
    by its contents -- its shape is free to evolve with the resolver without a
    migration, and no query needs to reach inside it.
    """

    __tablename__ = "organization_proposal"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'applying', 'applied', 'superseded', 'expired')",
            name="ck_organization_proposal_status",
        ),
        CheckConstraint(
            "source in ('organize', 'chat')",
            name="ck_organization_proposal_source",
        ),
        # At most one live proposal per memoir.
        #
        # The repository already supersedes earlier pending rows before
        # inserting, but that is two round-trips and therefore racy: two
        # concurrent organize runs can both find nothing pending and both
        # insert. This partial unique index makes the invariant a property of
        # the database rather than of the ordering of two HTTP calls, so a lost
        # race is a constraint violation instead of two confirmable proposals.
        Index(
            "uq_organization_proposal_one_pending",
            "memoir_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The confirm endpoint looks up "the pending proposal for this memoir"
        # on every request. Ascending `created_at` rather than DESC: a btree is
        # scanned in either direction, so `WHERE memoir_id = ? ORDER BY
        # created_at DESC` uses this index identically. Writing DESC would only
        # encode an intent that Postgres already satisfies, and would force this
        # to become an expression index that is awkward to reproduce in a
        # hand-written migration.
        Index("idx_organization_proposal_memoir_status", "memoir_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        # Server default, not just the Python-side one. Writes reach Postgres
        # through PostgREST, where `default=uuid.uuid4` never runs, so without
        # this an insert that omits `id` fails on NOT NULL. See the same note in
        # organization_job_run.py.
        server_default=text("gen_random_uuid()"),
    )
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=PROPOSAL_STATUS_PENDING)

    # The validated ResolvedPlan. Written only by the resolver, after every
    # memory_id has been checked against the exact set sent to the model.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # The memoir's organization_status when the proposal was produced, so a
    # stale proposal is recognisable as stale without re-deriving it.
    source_status: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="organize")

    # Short owner-facing label so the review UI can render something before it
    # has deserialized the payload.
    summary_line: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retained after application: a proposal stays explainable after the
    # chapters it produced have been hand-edited.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrganizationActionAudit(Base):
    """
    Append-only record of changes made to a memoir's chapters.

    `actor_user_id` is always resolved from the authenticated user server-side.
    Never a value from a request body -- the entire value of this table is that
    it records who actually did something.

    Deliberately small and typed rather than a full row snapshot. It answers
    "what changed and who did it", which is the question an audit exists for;
    attempting to snapshot every column turns a cheap append into an expensive
    one and still will not survive the next schema change.
    """

    __tablename__ = "organization_action_audit"
    __table_args__ = (
        CheckConstraint(
            "action_type in ('apply_proposal', 'rename_chapter', 'edit_chapter', "
            "'reorder_chapters', 'move_memory', 'chat')",
            name="ck_organization_action_audit_type",
        ),
        # Audit reads are always "everything for this memoir, newest first".
        # Ascending for the same btree reason as the proposal index above.
        Index("idx_organization_action_audit_memoir", "memoir_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Null when the action did not originate from a proposal (a manual reorder,
    # say), so this is a correlation id rather than an owning FK -- manual
    # actions are audited too.
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_proposal.id", ondelete="SET NULL"),
        nullable=True,
    )
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )