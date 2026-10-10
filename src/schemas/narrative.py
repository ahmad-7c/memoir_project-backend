"""
@file schemas/narrative.py
@description Pydantic contracts for the AI-composed biographical narrative.

Same two-family split as schemas/organization.py:

1. LLM response contracts (NarrativeSectionProposal, NarrativeGenerationOutput).
   What the model is allowed to return. `body` is AI-authored prose -- the
   bounded, explicitly-labelled exception to "the AI never writes memory
   content" -- and `source_memory_ids` is the citation list every validation
   rule in domain/narrative_service.py is built around.

2. HTTP request/response models, all `extra="forbid"`.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.organization import StrictSchemaModel

# Hard caps. Mirrors MAX_CHAPTERS/MAX_CHAPTER_SUMMARY in schemas/organization.py:
# the reference pattern in this codebase is to never trust a prompt's stated
# limit ("1 to 3 paragraphs") to be the only thing enforcing it.
MAX_SECTIONS_PER_CHAPTER = 6
MAX_TOTAL_SECTIONS = 80
MAX_SECTION_BODY = 4000
MAX_SOURCE_IDS_PER_SECTION = 50


class NarrativeSectionProposal(StrictSchemaModel):
    """
    One model-proposed narrative section for a single chapter's memories.

    `source_memory_ids` is REQUIRED and must be non-empty: the whole feature's
    governing rule is that every paragraph traces to real memories, so a
    section with zero citations is ungrounded text by definition and is
    rejected before anything is saved (see
    domain.narrative_service._validate_section_citations).
    """

    position: int = Field(ge=0)
    body: str = Field(max_length=MAX_SECTION_BODY)
    source_memory_ids: List[str] = Field(
        min_length=1,
        max_length=MAX_SOURCE_IDS_PER_SECTION,
        description="Memory UUIDs this section draws from. Must be a subset of the ids sent for its chapter.",
    )


class NarrativeGenerationOutput(StrictSchemaModel):
    sections: List[NarrativeSectionProposal] = Field(
        min_length=1, max_length=MAX_SECTIONS_PER_CHAPTER
    )


class NarrativeRejected(Exception):
    """
    Raised when the model's output can't be trusted and the ENTIRE generation
    must be discarded -- not just the offending chapter's sections.

    An invented memory_id means the model was not grounded for that call, and
    there is no way to tell which other sections (from other chapters, in the
    same run) might share the same failure mode. Saving the sections that
    "looked fine" would mean shipping an AI biography on a coin flip about
    which parts were actually grounded. See the module docstring on the
    governing rule.
    """


# ---------------------------------------------------------------------------
# HTTP request/response models
# ---------------------------------------------------------------------------


class GenerateNarrativeResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = "Narrative generation started in the background."
    status: str = "processing"


class NarrativeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    status: str = Field(description="none | processing | ready | failed | stalled")
    error_message: Optional[str] = None
    retry_available: bool = False
    # Owner-facing account of memories the narrative sent to the model but
    # which ended up cited by zero sections -- "a memory the narrative
    # skipped is a memory the family loses", surfaced rather than silent.
    warnings: List[str] = Field(default_factory=list)


class NarrativeSectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    memoir_id: str
    chapter_id: Optional[str] = None
    position: int
    body: str
    body_original: str
    owner_edited: bool
    source_memory_ids: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PublicNarrativeSectionResponse(BaseModel):
    """
    What a reader (or the owner, on the read-only published view) sees --
    deliberately excludes `body_original` and `owner_edited`, which are
    owner-side editorial facts about how the text came to be, not something
    a reader needs. Matches the public/private split already used for
    SharedMemoirResponse in schemas/share.py.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    chapter_id: Optional[str] = None
    position: int
    body: str
    source_memory_ids: List[str] = Field(default_factory=list)


class PublicNarrativeSectionsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    data: List[PublicNarrativeSectionResponse] = Field(default_factory=list)


class NarrativeSectionsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    data: List[NarrativeSectionResponse] = Field(default_factory=list)


class NarrativeSectionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = "Operation successful"
    data: NarrativeSectionResponse


class NarrativeSectionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_SECTION_BODY)


class RegenerateSectionResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = "Section regeneration started in the background."
    status: str = "processing"


class MarkReviewedResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = "Narrative marked as reviewed."
    narrative_reviewed_at: datetime


# --- The Sources expand: full, verbatim source memories for one section ---


class SourceMediaResponse(BaseModel):
    """
    Same allowlist discipline as SharedMediaAssetResponse in schemas/share.py:
    no storage_key, no internal ids. A reader who unlocked a share link sees
    this exact shape too.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    mime_type: Optional[str] = None
    caption: Optional[str] = None
    duration_ms: Optional[int] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    playback_url: Optional[str] = None
    transcript_text: Optional[str] = None


class SourceMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: Optional[str] = None
    body_text: Optional[str] = None
    occurred_start: Optional[str] = None
    author_name: Optional[str] = None
    media: List[SourceMediaResponse] = Field(default_factory=list)


class NarrativeSourcesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    section_id: str
    memories: List[SourceMemoryResponse] = Field(default_factory=list)
