r"""add AI-composed narrative sections with source attribution

Revision ID: b7d2a93f4c1e
Revises: a1c9e4f27b3d
Create Date: 2026-10-10 00:00:00.000000

Three tables plus one column for the AI-composed biographical narrative
feature:

  narrative_generation_run  -- one row per generate/regenerate attempt, so a
    slow background job can report real status (processing/ready/failed) and
    a stuck run can be detected as stalled. `section_id` is null for a
    whole-memoir generate and set for a single-section regenerate, so the
    status endpoint can tell the two apart. Mirrors organization_job_run
    (migrations/c3f9a1b204d7) in shape and rationale.

  narrative_section  -- the AI-written prose. `body_original` preserves
    exactly what the model produced even after an owner edits `body`, same
    principle as transcript.raw_text/display_text: never destroy the
    original of anything.

  narrative_source  -- composite-PK join table. Every section must cite at
    least one memory (enforced in the domain layer before insert, since a
    CHECK constraint can't see sibling rows); every memory id cited must
    belong to the same memoir as the section (enforced in the domain layer
    too, for the same reason -- not expressible as a CHECK across two tables).

  memoir.narrative_reviewed_at  -- null until the owner explicitly marks the
    narrative reviewed. memoir_service.publish_memoir checks this: a memoir
    with narrative sections and a null narrative_reviewed_at gets a 409.
    Publishing is irreversible and this text is AI-written, so nobody should
    be able to publish an unread AI biography of their dead parent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b7d2a93f4c1e"
down_revision: Union[str, None] = "a1c9e4f27b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memoir",
        sa.Column("narrative_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "narrative_section",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("memoir_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable: a section belongs to a chapter conceptually, but the FK is
        # ON DELETE CASCADE from chapter, and a chapter can be deleted (e.g. a
        # fresh organize run replaces AI-authored chapters) independently of
        # the narrative. Null means "chapter was removed after this section
        # was written" rather than silently deleting the prose too.
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_original", sa.Text(), nullable=False),
        sa.Column("owner_edited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["memoir_id"], ["memoir.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapter.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_narrative_section_memoir_position",
        "narrative_section",
        ["memoir_id", "position"],
        unique=False,
    )

    op.create_table(
        "narrative_source",
        sa.Column("narrative_section_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["narrative_section_id"], ["narrative_section.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["memory_id"], ["memory.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("narrative_section_id", "memory_id"),
    )
    # Reverse lookup: "which sections cite this memory" -- used to compute
    # which memories the narrative skipped (zero citing sections).
    op.create_index(
        "idx_narrative_source_memory", "narrative_source", ["memory_id"], unique=False
    )

    op.create_table(
        "narrative_generation_run",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("memoir_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Null = whole-memoir generate. Set = regenerating this one section
        # alone. ON DELETE SET NULL: deleting the section a regenerate run was
        # scoped to must not delete the run's own history row.
        sa.Column("section_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="processing", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Non-fatal findings from a successful run -- currently just which
        # memories were sent to the model but cited by zero sections. jsonb
        # rather than a normalised table: written once, read once, never
        # queried by contents, same reasoning as organization_proposal.payload.
        sa.Column(
            "warnings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('processing', 'ready', 'failed')",
            name="ck_narrative_generation_run_status",
        ),
        sa.CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_narrative_generation_run_time_order",
        ),
        sa.ForeignKeyConstraint(["memoir_id"], ["memoir.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["narrative_section.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_narrative_generation_run_memoir",
        "narrative_generation_run",
        ["memoir_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_narrative_generation_run_memoir", table_name="narrative_generation_run")
    op.drop_table("narrative_generation_run")
    op.drop_index("idx_narrative_source_memory", table_name="narrative_source")
    op.drop_table("narrative_source")
    op.drop_index("idx_narrative_section_memoir_position", table_name="narrative_section")
    op.drop_table("narrative_section")
    op.drop_column("memoir", "narrative_reviewed_at")
