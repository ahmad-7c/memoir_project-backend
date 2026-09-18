from datetime import datetime, date
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

ShareVisibility = Literal["private", "link", "password"]


class ShareLinkUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_at: Optional[datetime] = None
    visibility: Optional[ShareVisibility] = None
    password: Optional[str] = Field(
        None, description="Set/replace the unlock password. Never returned back to the owner."
    )
    clear_password: Optional[bool] = Field(
        None, description="If true, removes the password requirement (takes priority over `password`)."
    )

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class ShareLinkResponse(BaseModel):
    id: str
    memoir_id: str
    scope: str
    token: str
    url: str
    visibility: ShareVisibility
    has_password: bool
    created_by_participant_id: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    open_count: int


class ShareLinkResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Operation successful"
    data: ShareLinkResponse


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    password: str = ""

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        stripped = v.strip()
        if not (2 <= len(stripped) <= 60):
            raise ValueError("Please enter your name (2-60 characters).")
        return stripped


class UnlockResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    display_name: str
    can_comment: bool


class UnlockResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Operation successful"
    data: UnlockResponse


# --- Reader-facing memoir view (Fix 6: allowlisted output only) ---

class SharedMediaAssetResponse(BaseModel):
    """
    Deliberately excludes storage_key, storage_bucket, uploader_user_id and
    checksum_sha256 — readers get a signed playback URL, never a raw path or any
    internal identifier.
    """
    id: str
    kind: str
    mime_type: Optional[str] = None
    caption: Optional[str] = None
    duration_ms: Optional[int] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    playback_url: Optional[str] = None


class SharedMemoryResponse(BaseModel):
    """Excludes author_participant_id — no internal participant IDs reach a reader."""
    id: str
    title: Optional[str] = None
    body_text: Optional[str] = None
    occurred_start: Optional[date] = None
    occurred_end: Optional[date] = None
    occurred_precision: Optional[str] = None
    created_at: datetime
    media: List[SharedMediaAssetResponse] = []


class SharedMemoirResponse(BaseModel):
    """Excludes created_by_user_id and any other internal/owner-only memoir fields."""
    id: str
    subject_name: str
    subject_born_on: Optional[date] = None
    subject_died_on: Optional[date] = None
    subject_is_living: bool
    description: Optional[str] = None
    can_comment: bool
    memories: List[SharedMemoryResponse] = []


class SharedMemoirResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Operation successful"
    data: SharedMemoirResponse
