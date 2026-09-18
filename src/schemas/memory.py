"""
@file src/schemas/memory.py
@description Pydantic request and response schemas for memory creation and management,
enforcing strict status literals.
"""

import uuid
from datetime import date
from typing import Optional, Literal, List
from pydantic import BaseModel, Field

class MemoryCreateRequest(BaseModel):
    """
    Validation schema for creating a new memory within a memoir.
    """
    memoir_id: uuid.UUID = Field(..., description="UUID of the parent memoir container")
    title: Optional[str] = Field(None, max_length=255, description="Title of the memory")
    body_text: Optional[str] = Field(None, max_length=10000, description="Rich text content of the memory")
    
    # Enforce allowed status values via Literal to prevent typo 500 errors
    # Must match the public.memory_status Postgres enum exactly (draft/submitted) --
    # "saved" is not a valid value there and every write with it 500s at the DB layer.
    status: Literal["draft", "submitted"] = Field("draft", description="Publication status of the memory")
    
    # Use native date types instead of raw strings so 'tomorrow' or invalid strings fail with 422
    occurred_start: Optional[date] = Field(None, description="Start date of when the memory took place")
    occurred_end: Optional[date] = Field(None, description="End date of when the memory took place")
    
    # Restrict precision and source fields to allowed enums
    occurred_precision: Optional[Literal["day", "month", "year", "decade"]] = Field(
    "day", description="Precision level of the occurrence date"
    )
    
    date_source: Optional[Literal["owner", "contributor", "ai"]] = Field(
    "owner", 
    description="Source of the memory authoring"
)
    
    # Attached media assets
    media_asset_ids: Optional[List[uuid.UUID]] = Field(
        default_factory=list, description="List of media asset UUIDs linked to this memory"
    )