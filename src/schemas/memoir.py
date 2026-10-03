from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, Literal
from datetime import date
import uuid

class MemoirCreateRequest(BaseModel):
    """
    extra="forbid" and Literal-constrained enums.

    `visibility` and `comment_policy` were previously free-form strings with
    defaults, so a typo (or a probe) like "publick" or "invited_onlyy" was
    accepted and written straight to a Postgres enum column -- a 500 at the
    database layer rather than a 422 at the boundary. Constraining them to the
    real values is the fix.
    """

    model_config = ConfigDict(extra="forbid")

    subject_name: str = Field(..., max_length=200, description="Name of the subject of the memoir")
    subject_born_on: Optional[date] = Field(None, description="Birth date of the subject (YYYY-MM-DD)")
    subject_died_on: Optional[date] = Field(None, description="Death date of the subject (YYYY-MM-DD)")
    subject_is_living: bool = Field(False, description="Whether the subject is currently alive")
    description: Optional[str] = Field(None, max_length=5000, description="Optional description or blurb for the memoir")

    # Literal values transcribed from the live Postgres enums in
    # migrations/0000_bootstrap_sandbox_schema.sql:
    #   public.memoir_visibility = ('invited_only','link_with_password','link_public')
    #   public.comment_policy    = ('nobody','invited_only','anyone_who_can_view')
    #
    # These were previously free-form `str` fields, so a typo reached a Postgres
    # enum column and surfaced as a 500 from the database layer instead of a 422
    # at the boundary. Keep these lists in sync with the enums -- if the enum
    # gains a value, add it here in the same change.
    visibility: Literal["invited_only", "link_with_password", "link_public"] = Field(
        "invited_only", description="Who can view this memoir"
    )
    comment_policy: Literal["nobody", "invited_only", "anyone_who_can_view"] = Field(
        "invited_only", description="Who can comment on this memoir"
    )
    relationship: Optional[Literal[
    "self", "spouse_partner", "parent", "child", 
    "sibling", "grandchild", "extended_family", 
    "friend", "colleague", "neighbour", "other"
    ]] = Field("other", description="Relationship of the creator to the memoir subject")

    @model_validator(mode='after')
    def validate_memoir_constraints(self) -> 'MemoirCreateRequest':
        if self.subject_born_on and self.subject_died_on:
            if self.subject_born_on > self.subject_died_on:
                raise ValueError("Subject birth date cannot be after their death date.")
        if self.subject_is_living and self.subject_died_on is not None:
            raise ValueError("A living subject cannot have a death date.")
        return self


class MemoirResponseData(BaseModel):
    """The raw memoir record returned inside the data envelope."""
    id: uuid.UUID
    subject_name: str
    subject_born_on: Optional[date] = None
    subject_died_on: Optional[date] = None
    subject_is_living: bool
    description: Optional[str] = None
    visibility: str
    comment_policy: str
    created_by_user_id: uuid.UUID
    status: str


class MemoirResponseEnvelope(BaseModel):
    """Consistent API response envelope for frontend consumption and OpenAPI documentation."""
    success: bool = True
    message: str = "Operation successful"
    data: MemoirResponseData