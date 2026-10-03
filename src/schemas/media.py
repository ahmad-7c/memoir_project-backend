"""
@file schemas/media.py
@description Pydantic validation schemas for presigned URL generation and media metadata,
enforcing strict UUID typing, file size limits, media kinds, and path traversal protection on filenames.
"""

import os
import uuid
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PresignedUrlRequest(BaseModel):
    """
    Validation schema for requesting a direct-to-storage presigned upload URL.

    extra="forbid": this schema mints a signed upload URL, so every field is a
    security boundary. Silently ignoring an unrecognized one means a client
    sending an unexpected field gets a URL for a request nobody reviewed.
    """

    model_config = ConfigDict(extra="forbid")
    memoir_id: uuid.UUID = Field(..., description="UUID of the parent memoir")
    filename: str = Field(..., description="Original name of the file being uploaded")
    
    # Aliases 'file_type' from incoming payloads to 'mime_type' for compatibility
    mime_type: str = Field(
        ..., 
        validation_alias="file_type", 
        description="MIME type of the file (e.g., image/jpeg, audio/webm)"
    )
    
    # byte_size: int = Field(..., gt=0, description="Size of the file in bytes")

    @field_validator('filename')
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        """
        Sanitizes incoming filenames to strip directory traversal characters 
        (e.g., '../', absolute paths) and retain only a safe base filename.

        Args:
            v (str): The raw input filename.

        Returns:
            str: The sanitized base filename.

        Raises:
            ValueError: If the filename contains path traversal attempts or is empty.
        """
        safe_name = os.path.basename(v.strip())
        if not safe_name or ".." in v or "/" in v or "\\" in v:
            raise ValueError("Invalid or unsafe filename provided.")
        return safe_name


class MediaMetadataRequest(BaseModel):
    """
    Validation schema for persisting media asset metadata after a successful upload.

    extra="forbid" for the same reason as PresignedUrlRequest, and because
    storage_key is the one field that decides where bytes live in the bucket --
    an unexpected extra field here is worth rejecting loudly.
    """

    model_config = ConfigDict(extra="forbid")

    memoir_id: uuid.UUID = Field(..., description="UUID of the parent memoir")

    # Length-bounded: storage_key and mime_type both flow into Supabase queries
    # and headers respectively. media_service re-validates the key's prefix
    # (must start with memoirs/<memoir_id>/), but the bound here is the first
    # line of defence against an oversized or hostile value.
    storage_key: str = Field(..., max_length=512, description="Storage path key in Supabase storage")
    kind: str = Field(..., max_length=16, description="Media kind: 'photo', 'audio', or 'video'")
    mime_type: str = Field(..., max_length=128, description="MIME type of the file")
    byte_size: int = Field(..., gt=0, le=10 * 1024 * 1024 * 1024, description="Size of the file in bytes")
    original_filename: Optional[str] = Field(None, max_length=255, description="Original filename")
    duration_ms: Optional[int] = Field(None, ge=0, description="Duration in milliseconds (null for photos)")
    width_px: Optional[int] = Field(None, ge=0, description="Width in pixels (null for audio)")
    height_px: Optional[int] = Field(None, ge=0, description="Height in pixels (null for audio)")
    caption: Optional[str] = Field(None, max_length=1000, description="Optional caption for the media")
    checksum_sha256: Optional[str] = Field(None, min_length=64, max_length=64, description="Optional file checksum")
    
    @field_validator('kind')
    @classmethod
    def validate_media_kind(cls, v: str) -> str:
        """
        Ensures media kind is within the permitted classification set.
        """
        allowed_kinds = {'photo', 'audio', 'video'}
        if v not in allowed_kinds:
            raise ValueError(f"Invalid media kind '{v}'. Must be one of {allowed_kinds}.")
        return v