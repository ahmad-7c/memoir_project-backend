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
    # Stored in PLAINTEXT, deliberately -- the owner must be able to re-copy
    # the exact link months later (there is no "resend" flow), so a hash
    # that can't be reversed back into the link isn't viable here. This is
    # the one place in the product's auth model where a credential sits in
    # the database unhashed, and it is compensated for rather than ignored:
    # generated with secrets.token_urlsafe(32) (256 bits, cryptographically
    # unpredictable, never sequential), fully revocable (revoked_at), and
    # never written to any log line. A reader also needs the password
    # (hashed, see password_hash below) to do anything with it, so this
    # token alone is "where to knock", not "the key".
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
