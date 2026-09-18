import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text, BigInteger, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models import _enums


class Memoir(Base):
    __tablename__ = "memoir"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_name: Mapped[str] = mapped_column(Text, nullable=False)
    subject_born_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    subject_died_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    subject_is_living: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_media_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_asset.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(_enums.memoir_status, nullable=False, server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    visibility: Mapped[str] = mapped_column(_enums.memoir_visibility, nullable=False, server_default="invited_only")
    view_password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_policy: Mapped[str] = mapped_column(_enums.comment_policy, nullable=False, server_default="invited_only")
    video_bytes_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_account.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
