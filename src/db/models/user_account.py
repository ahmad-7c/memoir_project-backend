import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class UserAccount(Base):
    __tablename__ = "user_account"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    # No billing provider is integrated yet (Subscription.tsx is a mock payment
    # form) -- this column exists so the owner profile has something real to
    # read today, and a real billing integration later only needs to start
    # writing to it instead of inventing a new field + migration.
    subscription_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="free")
    auth_provider_uid: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
