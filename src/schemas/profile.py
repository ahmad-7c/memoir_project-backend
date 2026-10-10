"""
@file schemas/profile.py
@description Response shape for GET /me/profile -- the owner profile page.
"""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class OwnerMemoirSummary(BaseModel):
    id: str
    subject_name: str
    subject_born_on: Optional[date] = None
    subject_died_on: Optional[date] = None
    subject_is_living: bool
    cover_image_url: Optional[str] = None
    status: Literal["draft", "published"]
    published_at: Optional[datetime] = None
    memory_count: int
    updated_at: datetime


class OwnerProfileResponse(BaseModel):
    name: str
    email: str
    subscription_status: str
    memoirs: List[OwnerMemoirSummary]


class OwnerProfileResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Operation successful"
    data: OwnerProfileResponse
