import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class NarrativeGenerationRun(Base):
    """
    One narrative generate/regenerate attempt. Mirrors organization_job_run
    in shape: the memoir row alone can't answer "where is this run right now",
    and BackgroundTasks has no heartbeat, so a dead process would otherwise
    leave status at 'processing' forever. `compute_effective_narrative_status`
    overrides a 'processing' row past the stall threshold at read time.
    """

    __tablename__ = "narrative_generation_run"
    __table_args__ = (
        CheckConstraint(
            "status in ('processing', 'ready', 'failed')",
            name="ck_narrative_generation_run_status",
        ),
        CheckConstraint(
            "completed_at is null or completed_at >= started_at",
            name="ck_narrative_generation_run_time_order",
        ),
        Index("idx_narrative_generation_run_memoir", "memoir_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    # Null = whole-memoir generate; set = a single-section regenerate.
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("narrative_section.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="processing")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
