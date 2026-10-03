"""
@file schemas/export.py
@description Pydantic schemas for memoir export request and job status responses.
"""

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Literal
from datetime import datetime

class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Literal rather than str: `kind` selects the export renderer. An
    # unrecognized value would otherwise flow into a renderer lookup and fail
    # there as a 500 instead of a 422 at the boundary. 'pdf' is the only kind
    # export_service implements today -- add to this Literal in the same change
    # that adds a renderer.
    kind: Literal["pdf"] = Field(default="pdf", description="Export format kind")

class ExportJobResponse(BaseModel):
    export_id: str
    memoir_id: str
    status: str
    message: str
    storage_key: Optional[str] = None
    download_url: Optional[str] = None
    created_at: datetime