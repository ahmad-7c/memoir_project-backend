"""
@file schemas/comment_schemas.py
@description Pydantic validation schemas for comment requests and responses.
"""

from pydantic import BaseModel
from typing import Optional

class CommentCreate(BaseModel):
    """
    Deliberately NOT `extra="forbid"`, unlike most request schemas in this
    codebase -- see the comments service's own docstring (THE rule: author
    name is read from the signed caller credential, never the body). A
    client sending `author_name` here must have it silently dropped, not
    rejected with a 422 that would just invite retrying with a different
    field name. Pydantic's default `extra="ignore"` is exactly that
    behaviour, so the absence of `extra="forbid"` here is intentional, not
    an oversight.
    """

    memoir_id: str
    # Exactly one of these two must be set -- enforced in
    # CommentsService.create_new_comment, where the richer "not both, not
    # neither" rule can raise a clear error instead of being implicit here.
    memory_id: Optional[str] = None  # Using str instead of strict UUID prevents version 4 errors on test/mock IDs (JUST FOR MOCK DATA)
    narrative_section_id: Optional[str] = None
    media_asset_id: Optional[str] = None
    parent_comment_id: Optional[str] = None
    body: str


class CommentResponse(BaseModel):
    id: str
    memoir_id: str
    memory_id: Optional[str] = None
    narrative_section_id: Optional[str] = None
    media_asset_id: Optional[str] = None
    author_participant_id: Optional[str] = None  # null for reader-authored comments (no account)
    parent_comment_id: Optional[str] = None
    body: str
    created_at: str
    updated_at: Optional[str] = None
    # No placeholder default: always the owner's account name or the reader's
    # captured display name. Null only for a genuine pre-existing data gap.
    author_name: Optional[str] = None

    class Config:
        orm_mode = True
