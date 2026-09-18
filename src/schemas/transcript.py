"""
@file transcript.py
@description Pydantic schemas for audio transcription validation and requests.
"""

from pydantic import BaseModel, Field

class TranscriptionRequest(BaseModel):
    media_asset_id: str = Field(..., description="The UUID of the audio media asset")
    # memoir_id and storage_key are intentionally NOT accepted from the client.
    # Both are resolved server-side from the media_asset row to prevent path
    # traversal / cross-tenant transcription requests.