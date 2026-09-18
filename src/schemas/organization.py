"""
@file schemas/organization.py
@description Pydantic schemas for AI chapter organization: the LLM output
contract and the owner-facing request/response models.
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

DatePrecision = Literal["day", "month", "year", "decade"]


class MemoryMapping(BaseModel):
    """
    One memory placed into a chapter by the model. Deliberately carries no
    field the model could use to write or invent text (R3) -- only an ID it
    was given back, and an optional date-precision guess.
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(description="The exact UUID of the memory, must be one we sent.")
    inferred_date: Optional[DatePrecision] = Field(
        default=None, description="Precision of the memory's date, if inferable."
    )


class ChapterOutput(BaseModel):
    """
    A single proposed chapter. No summary/narrative field on purpose: per R3
    the response schema must contain only a title, an ordering, and memory
    IDs -- there is nowhere here to put rewritten or merged memory text.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200, description="A short, chronological chapter title.")
    sort_order: int = Field(ge=0, description="The chronological sequence order.")
    memories: List[MemoryMapping] = Field(description="Memories assigned to this chapter.")


class MemoirOrganizationOutput(BaseModel):
    """
    Top-level LLM response contract. Chapter count is bounded here (R4:
    'roughly 2 to 12') -- everything else about trusting the *content* of
    this object (do the IDs actually exist, is any ID duplicated) has to
    happen in code after parsing, since structured output only guarantees
    shape, not content.

    Lower bound is 1, not 2: a memoir with only one or two memories (every
    brand-new user's first "Organize" click) can only ever produce a single
    honest chapter -- requiring 2+ would reject that as untrustworthy when
    it's actually correct. The upper bound of 12 is what actually guards
    against a degenerate/runaway response.
    """

    model_config = ConfigDict(extra="forbid")

    chapters: List[ChapterOutput] = Field(
        min_length=1, max_length=12, description="Chronological chapters covering the provided memories."
    )


class OrganizeResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Organization started in the background."
    status: str = "processing"


class OrganizeStatusResponse(BaseModel):
    success: bool = True
    status: str = Field(description="none | queued | running | ready | failed | stalled")
    error_message: Optional[str] = None
    retry_available: bool = False


class ChapterUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, min_length=1, max_length=200, description="The new title chosen by the owner.")
    summary: Optional[str] = Field(None, max_length=2000, description="The new summary chosen by the owner.")


class MemoryMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_chapter_id: str = Field(description="The ID of the chapter this memory should be moved to.")


class ChapterOrderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    sort_order: int = Field(ge=0)


class ChapterReorderRequest(BaseModel):
    """R10 requires the owner be able to reorder chapters -- missing entirely
    from the reference branch, added here."""

    model_config = ConfigDict(extra="forbid")

    order: List[ChapterOrderEntry] = Field(min_length=1, description="The full new chapter ordering.")


class ChatMessage(BaseModel):
    """
    One turn of prior conversation, echoed back by the client each request
    (the server holds no chat history of its own). Length-bounded so a
    caller can't balloon the prompt sent to Gemini on every turn.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(description="Who said this turn.")
    content: str = Field(min_length=1, max_length=4000, description="The text content of the message.")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000, description="The user's latest prompt for the AI co-author.")
    history: List[ChatMessage] = Field(
        default_factory=list, max_length=40, description="Past conversation turns for context."
    )


class ChatResponse(BaseModel):
    success: bool = True
    reply: str = Field(description="Gemini's contextual response regarding the archive.")
