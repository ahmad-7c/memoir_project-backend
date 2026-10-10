import uuid

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class NarrativeSource(Base):
    """
    Composite-PK join: which memories a narrative section was derived from.
    This is the record that makes the Sources button's claim checkable --
    "this paragraph comes from exactly these memories" is a fact in the
    database, not something the frontend has to trust the AI about after the
    fact.
    """

    __tablename__ = "narrative_source"
    __table_args__ = (
        Index("idx_narrative_source_memory", "memory_id"),
    )

    narrative_section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("narrative_section.id", ondelete="CASCADE"), primary_key=True
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory.id", ondelete="CASCADE"), primary_key=True
    )
