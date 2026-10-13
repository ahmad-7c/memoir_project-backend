"""
@file src/schemas/memory.py
@description Pydantic request and response schemas for memory creation and management,
enforcing strict status literals.
"""

import uuid
from datetime import date
from typing import Optional, Literal, List
from pydantic import BaseModel, ConfigDict, Field

class MemoryCreateRequest(BaseModel):
    """
    Validation schema for creating a new memory within a memoir.

    extra="forbid" so an unrecognized field is a 422 rather than silently
    dropped. Without it a typo like `occurred_precison` fails quietly and the
    memory saves with a default the author didn't choose -- and an unexpected
    extra field slips through unexamined, which is exactly what you don't want
    on the path that writes a family's own words.
    """

    model_config = ConfigDict(extra="forbid")
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
    
    # Attached media assets. Bounded: an unbounded list here is a free
    # unlimited-size body on the path that writes a memory.
    media_asset_ids: Optional[List[uuid.UUID]] = Field(
        default_factory=list, max_length=50, description="List of media asset UUIDs linked to this memory"
    )


class MemoryUpdateRequest(BaseModel):
    """
    Partial update of an existing memory.

    Exists so the capture flow can save the author's words FIRST and attach
    media afterwards. Before this, the only write path was `POST /api/memories`
    with every attachment supplied at creation, which forced the UI to upload
    files before saving text -- so a dropped connection mid-upload lost the
    typed words entirely.

    Every field is optional and only supplied fields are written: omitting
    `title` leaves the stored title alone, which is what a draft autosave of
    just the body needs. `media_asset_ids` is additive on purpose (see the
    service) -- a PATCH that sent the full list each time would make "add one
    photo" a destructive operation if the client's view of the list were stale.

    `memoir_id` is NOT accepted. A memory cannot be moved between memoirs by
    editing it; that would be a cross-tenant write dressed up as an update.
    Moving between *chapters* has its own endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, max_length=255)
    body_text: Optional[str] = Field(None, max_length=10000)
    status: Optional[Literal["draft", "submitted"]] = None
    occurred_start: Optional[date] = None
    occurred_end: Optional[date] = None
    occurred_precision: Optional[Literal["day", "month", "year", "decade"]] = None
    media_asset_ids: Optional[List[uuid.UUID]] = Field(
        default=None, max_length=50, description="Media asset UUIDs to ATTACH (additive, not a replacement set)"
    )