r"""add server-held organization proposals and the action audit trail

Revision ID: e7a41c5b9f32
Revises: c3f9a1b204d7
Create Date: 2026-10-03 10:40:00.000000

Two tables: `organization_proposal` (a validated plan awaiting owner
confirmation) and `organization_action_audit` (an append-only record of who
changed what).

WHY A PROPOSAL TABLE AT ALL

The reference implementation's apply endpoint took the entire chapter structure
as a request body:

    class ChapterApplyPayload(BaseModel):
        chapters: List[Dict[str, Any]]

...and wrote it to the database with no ownership check and no `memoir_id` scope
on the per-memory update. Any authenticated user could re-point another family's
memories into their own memoir. No amount of validating that body fixes it: the
body *is* the attack surface.

The fix is structural. Invert who holds the plan -- the server persists a
validated proposal, the client holds only its id. `ApplyProposalRequest` is
`{proposal_id: str}` and there is no field in it that could influence what gets
written, which is precisely why there is nothing left to validate at that
endpoint.

It also buys the audit trail the reference had no concept of. If a chapter ever
looks wrong, there is a record of which run produced it, which owner confirmed
it, and what the plan contained at the time.

THE `applying` STATUS AND THE PARTIAL UNIQUE INDEX

Both exist to make concurrent confirmation safe.

1. `applying` gives `confirm_proposal` somewhere to move the row before it
   starts writing. Without an in-flight state, two confirmations of the same
   proposal both read `status = 'pending'`, both write the full chapter set, and
   the memoir ends up with duplicate chapters and an audit trail showing one
   apply. The claim is a conditional UPDATE (`WHERE status = 'pending'`
   returning rows) -- a compare-and-swap exactly one caller can win. If the
   process dies while `applying`, the row is stranded but recoverable: the same
   age check that reaps a stale `pending` row reaps a stranded `applying` one.

2. `uq_organization_proposal_one_pending` is a partial unique index over
   `memoir_id WHERE status = 'pending'`. The repository already supersedes
   earlier pending rows before inserting, but that is two round-trips and
   therefore racy -- two concurrent organize runs can each find nothing pending
   and each insert. The index makes "at most one live proposal per memoir" a
   property of the database rather than of the ordering of two HTTP calls, so a
   lost race becomes a constraint violation instead of two confirmable plans.

RLS is enabled with no policies, matching every other table in this schema. The
application uses the service-role client, which bypasses RLS, after verifying
ownership itself. Enabled rather than omitted so that a future anon-key client
sees nothing rather than every family's organize history.

This supersedes the hand-written migrations/0007_organization_proposals.sql,
which was never applied. That file has been deleted rather than left in place:
two files describing one schema, one of them dead, is how the next person ends
up not knowing which one runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "e7a41c5b9f32"
down_revision: Union[str, None] = "c3f9a1b204d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_proposal",
        # Server default, not decoration: writes reach this table through
        # PostgREST, where a Python-side default never runs.
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("memoir_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        # jsonb rather than normalised rows: the plan is written once, read once,
        # and never queried by its contents, so its shape is free to evolve with
        # the resolver without a migration.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_status", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default="organize", nullable=False),
        sa.Column("summary_line", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status in ('pending', 'applying', 'applied', 'superseded', 'expired')",
            name="ck_organization_proposal_status",
        ),
        sa.CheckConstraint(
            "source in ('organize', 'chat')", name="ck_organization_proposal_source"
        ),
        sa.ForeignKeyConstraint(["memoir_id"], ["memoir.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_organization_proposal_memoir_status",
        "organization_proposal",
        ["memoir_id", "status", "created_at"],
        unique=False,
    )
    # Partial unique index: at most one confirmable proposal per memoir. See the
    # module docstring for why this has to live in the database rather than in
    # the repository's insert ordering.
    op.create_index(
        "uq_organization_proposal_one_pending",
        "organization_proposal",
        ["memoir_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "organization_action_audit",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        # SET NULL, not CASCADE: deleting a proposal must not erase the history
        # of what was applied. The correlation id is allowed to go dangling; the
        # record of the action is not.
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("memoir_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Always resolved from the authenticated user server-side, never taken
        # from a request body. That is the entire value of this table.
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column(
            "target_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "action_type in ('apply_proposal', 'rename_chapter', 'edit_chapter', "
            "'reorder_chapters', 'move_memory', 'chat')",
            name="ck_organization_action_audit_type",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["organization_proposal.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["memoir_id"], ["memoir.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_organization_action_audit_memoir",
        "organization_action_audit",
        ["memoir_id", "created_at"],
        unique=False,
    )

    op.execute("alter table public.organization_proposal enable row level security")
    op.execute("alter table public.organization_action_audit enable row level security")

    op.execute(
        "comment on table public.organization_proposal is "
        "'Owner-reviewed organization plans. The client holds only a proposal id; "
        "the plan itself never travels in a request body.'"
    )
    op.execute(
        "comment on column public.organization_proposal.payload is "
        "'A validated ResolvedPlan. Written only after every memory_id was checked "
        "against the exact set sent to the model.'"
    )
    op.execute(
        "comment on table public.organization_action_audit is "
        "'Append-only record of changes made to a memoir''s chapters, with the "
        "acting user resolved server-side.'"
    )


def downgrade() -> None:
    op.drop_index("idx_organization_action_audit_memoir", table_name="organization_action_audit")
    op.drop_table("organization_action_audit")

    op.drop_index("uq_organization_proposal_one_pending", table_name="organization_proposal")
    op.drop_index(
        "idx_organization_proposal_memoir_status", table_name="organization_proposal"
    )
    op.drop_table("organization_proposal")