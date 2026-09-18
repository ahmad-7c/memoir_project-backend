import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models import _enums


class MemoirLink(Base):
    __tablename__ = "memoir_link"
    __table_args__ = (
        CheckConstraint("visibility in ('private', 'link', 'password')", name="memoir_link_visibility_check"),
        Index("idx_memoir_link_memoir_id", "memoir_id"),
        Index("idx_memoir_link_token", "token"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memoir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(_enums.link_scope, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_participant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memoir_participant.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Added by the reader-password fix (src/domain/share_service.py):
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="password")
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
