"""
@file transcript.py
@description Pydantic schemas for audio transcription validation and requests.
"""

from pydantic import BaseModel, ConfigDict, Field

class TranscriptionRequest(BaseModel):
    """
    extra="forbid" matters more here than anywhere else in the codebase.

    The comment below explains why memoir_id and storage_key are deliberately
    not accepted: they are resolved server-side from the media_asset row, which
    is what prevents a caller from transcribing another tenant's file. With
    extra="forbid", sending either is a loud 422 rather than a silently dropped
    field -- so a client that (wrongly) sends storage_key finds out, instead of
    believing it worked while the server used its own resolved value.
    """

    model_config = ConfigDict(extra="forbid")

    media_asset_id: str = Field(..., max_length=64, description="The UUID of the audio media asset")
    # memoir_id and storage_key are intentionally NOT accepted from the client.
    # Both are resolved server-side from the media_asset row to prevent path
    # traversal / cross-tenant transcription requests.