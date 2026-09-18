import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models import _enums


class Memory(Base):
    __tablename__ = "memory"
    __table_args__ = (
        Index("idx_memory_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("idx_memory_memoir_id", "memoir_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    # No FK in the live schema — faithfully replicated, not an oversight introduced here.
    author_participant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(_enums.memory_status, nullable=False, server_default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    position_in_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    occurred_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    occurred_precision: Mapped[str | None] = mapped_column(_enums.date_precision, nullable=True)
    date_source: Mapped[str | None] = mapped_column(_enums.authored_by, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_participant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir_participant.id", ondelete="SET NULL"), nullable=True
    )
    search_tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
