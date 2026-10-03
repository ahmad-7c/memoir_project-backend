"""
@file schemas/organization.py
@description Pydantic contracts for AI chapter organization.

Two families of models live here and the distinction matters:

1. **LLM response contracts** (MemoryFact, ChapterProposal, OrganizationPlan).
   These are what a model is allowed to return. Under invariant 3 in the root
   AGENTS.md they carry no field that would let the model rewrite a memory's
   text — except ChapterProposal.summary, whose entire purpose is AI-authored
   biographical prose and which is therefore bounded and explicitly marked.

2. **Request/response models** for the HTTP surface.

Every request model sets `extra="forbid"`. A request body that reaches a
database write is the highest-severity bug class in this codebase, and
`Dict[str, Any]` on such a body has happened before.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DatePrecision = Literal["day", "month", "year", "decade"]

# Hard caps applied to every model response. The reference implementation
# trusted the prompt ("create 3 to 6 chapters") and enforced nothing, so a
# runaway response could write unbounded rows.
MAX_CHAPTERS = 12
MIN_CHAPTERS = 1

# The organizer gets one fewer slot than the plan may hold, because the resolver
# may need to append a catch-all chapter for memories the model would not
# confidently place. "Nothing is silently dropped" is a hard rule, so the
# catch-all is not optional — and if the organizer could fill all 12 slots there
# would be nowhere to put it.
#
# This was a real defect: `OrganizerOutput` and `ResolvedPlan` both allowed 12
# and 13 chapters respectively, while `ChapterReorderRequest.order` is bounded
# at `max_length=MAX_CHAPTERS`. The system could therefore produce a memoir it
# was then unable to reorder, returning 422 to the owner on their own book.
MAX_ORGANIZED_CHAPTERS = MAX_CHAPTERS - 1

MAX_CHAPTER_TITLE = 200
MAX_CHAPTER_SUMMARY = 4000
MAX_FACT_GIST = 300


class StrictSchemaModel(BaseModel):
    """
    Base for models that may be sent to a provider with strict Structured
    Outputs.

    Groq's strict mode requires every field to be `required` and sets
    `additionalProperties: false`. Pydantic models with `Optional` fields that
    carry defaults do not satisfy that, and the request fails with a 400 at
    runtime — a confusing failure that looks like a model problem but is a
    schema problem.

    Rather than contorting the Python-side models (which would make them worse
    to construct and validate), each strict-capable model supplies a companion
    `strict_json_schema()` that reshapes the schema: optional fields become
    nullable-and-required, defaults are stripped.

    Nullable is the correct encoding for "the model may omit this": it is
    semantically identical to Optional for our purposes, and it is expressible
    under strict mode.
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def strict_json_schema(cls) -> Dict[str, Any]:
        raw = cls.model_json_schema()
        return _make_strict_compatible(raw)


def _make_strict_compatible(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively rewrites a JSON schema so it satisfies strict-mode rules:
    every object gets `additionalProperties: false`, every property becomes
    required, and `$defs`/`$ref` are preserved because Pydantic emits them for
    any reused model.
    """
    if not isinstance(schema, dict):
        return schema

    result: Dict[str, Any] = {}

    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            # Recurse into each property. Assigning `value` directly (the
            # obvious-looking version) leaves nested `default`/`title` keys in
            # place, which strict mode rejects — and the failure surfaces at
            # request time as a 400 that reads like a model problem.
            result["properties"] = {
                prop_name: _make_strict_compatible(prop_schema)
                for prop_name, prop_schema in value.items()
            }
            # Everything required. A field the model genuinely can't fill is
            # declared nullable in the model, which strict mode accepts.
            #
            # Assigned unconditionally rather than via setdefault: Pydantic emits
            # its own `required` key (only the non-defaulted fields), and since
            # it appears *after* `properties` in the dict, letting it through
            # would silently overwrite the strict-mode list and leave optional
            # fields out — producing a schema the provider rejects with a 400
            # that looks like a model problem.
            result["required"] = list(value.keys())
        elif key in ("default", "title", "description", "required"):
            # Titles/descriptions are noise for strict decoding; `required` is
            # recomputed above for every object we rewrite.
            continue
        elif isinstance(value, dict):
            result[key] = _make_strict_compatible(value)
        elif isinstance(value, list):
            result[key] = [_make_strict_compatible(item) for item in value]
        else:
            result[key] = value

    if "properties" in result and "additionalProperties" not in result:
        result["additionalProperties"] = False

    return result


class MemoryFact(StrictSchemaModel):
    """
    Stage 1 (Reader) output: one compact fact per memory.

    `gist` is a short paraphrase written by the model. This is the one place
    paraphrase is acceptable, and it is bounded: it never overwrites
    memory.body_text, it is advisory text shown in the proposal review UI, and
    the original memory text remains the source of truth. See root AGENTS.md
    §3.3.
    """

    memory_id: str = Field(description="The exact UUID of the memory; must be one we sent.")
    title: Optional[str] = Field(default=None, max_length=MAX_CHAPTER_TITLE)
    occurred_start: Optional[str] = Field(default=None, description="YYYY-MM-DD or null.")
    era: Optional[str] = Field(default=None, max_length=100, description="e.g. '1980s', 'Childhood'.")
    topic: Optional[str] = Field(default=None, max_length=200)
    gist: str = Field(default="", max_length=MAX_FACT_GIST, description="At most ~25 words.")


class ChapterProposal(StrictSchemaModel):
    """
    Stage 2 (Organizer) output: one proposed chapter.

    `summary` is AI-authored biographical prose — the deliberate, bounded
    exception to invariant 3. It is written to chapter.summary, never to
    memory.body_text, it is capped here, and it is owner-editable before it
    becomes visible to share-link readers.
    """

    title: str = Field(min_length=1, max_length=MAX_CHAPTER_TITLE)
    summary: str = Field(
        default="",
        max_length=MAX_CHAPTER_SUMMARY,
        description="AI-authored biographical prose. Empty is valid and preferred over fabrication.",
    )
    era_label: Optional[str] = Field(default=None, max_length=100)
    memory_ids: List[str] = Field(description="Memory UUIDs placed in this chapter, chronological.")


class RefinedProposal(StrictSchemaModel):
    """Stage 3 (Refiner) output — same shape, driven by an owner chat request."""

    # Same reservation as OrganizerOutput: the refiner can also leave memories
    # unplaced, and `refine_organization_plan` resolves its output through the
    # same path that appends a catch-all.
    chapters: List[ChapterProposal] = Field(
        min_length=MIN_CHAPTERS, max_length=MAX_ORGANIZED_CHAPTERS
    )


class ReaderOutput(StrictSchemaModel):
    facts: List[MemoryFact] = Field(description="One fact per memory sent.")


class OrganizerOutput(StrictSchemaModel):
    chapters: List[ChapterProposal] = Field(
        min_length=MIN_CHAPTERS, max_length=MAX_ORGANIZED_CHAPTERS
    )


# ---------------------------------------------------------------------------
# Resolved plan — produced by deterministic Python, never by an LLM.
# ---------------------------------------------------------------------------


class ResolvedMemoryPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    chapter_index: int = Field(ge=0)
    position: int = Field(ge=0)
    inferred_date: Optional[DatePrecision] = None


class ResolvedChapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_CHAPTER_TITLE)
    summary: str = Field(default="", max_length=MAX_CHAPTER_SUMMARY)
    era_label: Optional[str] = None
    sort_order: int = Field(ge=0)
    memory_ids: List[str] = Field(default_factory=list)


class ResolvedPlan(BaseModel):
    """
    The only structure permitted to reach the persistence layer.

    Constructed by the resolver after every model response has been validated
    against the exact set of memory ids that was sent. Nothing downstream
    re-reads a raw model response.
    """

    model_config = ConfigDict(extra="forbid")

    # Bounded, because this is the structure that reaches the database and the
    # book. The resolver guarantees the bound by construction (the organizer is
    # capped at MAX_ORGANIZED_CHAPTERS to leave room for the catch-all), so a
    # plan that violates it is a resolver bug and should fail loudly here rather
    # than write 13 chapters to a memoir whose reorder endpoint accepts 12.
    chapters: List[ResolvedChapter] = Field(
        min_length=MIN_CHAPTERS, max_length=MAX_CHAPTERS
    )
    unplaced_memory_ids: List[str] = Field(
        default_factory=list,
        description="Memories the model didn't confidently place. Never silently dropped.",
    )

    def all_assigned_ids(self) -> set:
        assigned = {mid for chapter in self.chapters for mid in chapter.memory_ids}
        return assigned


class ChapterProposalReview(BaseModel):
    """
    What the owner actually sees before confirming.

    Wraps the resolved plan with a human-readable account of what changed, so
    the confirm step is a real review rather than a blind "apply".
    """

    model_config = ConfigDict(extra="forbid")

    plan: ResolvedPlan
    warnings: List[str] = Field(default_factory=list)
    memories_considered: int = Field(ge=0)
    memories_placed: int = Field(ge=0)
    memories_unplaced: int = Field(ge=0)


class OrganizationRejected(Exception):
    """
    Raised when a model response can't be trusted and must be discarded whole.

    Whole-response rejection is deliberate: an unknown memory id means the
    model was confused, so *nothing* it produced can be relied on. Partially
    applying a confused response is how a memoir ends up with three chapters
    from one run and a silent gap from another.
    """


# ---------------------------------------------------------------------------
# HTTP request/response models
# ---------------------------------------------------------------------------


class OrganizeResponseEnvelope(BaseModel):
    success: bool = True
    message: str = "Organization started in the background."
    status: str = "processing"


class OrganizeStage(BaseModel):
    """One pipeline stage, as recorded by `organization_agent_run`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Pipeline stage name: reader | organizer | refiner | resolver")
    status: str = Field(description="running | ready | failed")


class OrganizeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    status: str = Field(description="none | queued | running | ready | failed | stalled")
    error_message: Optional[str] = None
    retry_available: bool = False
    current_stage: Optional[str] = Field(
        default=None,
        description="Name of the stage currently executing, e.g. 'reader'. None if unknown.",
    )
    # Structured rather than a list of display strings, so the client can render
    # per-stage state instead of guessing at it. The previous shape was a fixed
    # list of four English phrases that never changed, which meant the UI could
    # not tell "reading memories" from "writing summaries".
    stages: List[OrganizeStage] = Field(default_factory=list)
    # Which provider actually served the run. Surfaced to the owner because a
    # persistently misconfigured chain looks identical to a healthy one from the
    # outside, and this is the only place that fact is visible. It names a
    # provider, not a key, and no request or response body is ever logged with it.
    provider_used: Optional[str] = None


class ChapterUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, min_length=1, max_length=MAX_CHAPTER_TITLE)
    summary: Optional[str] = Field(None, max_length=MAX_CHAPTER_SUMMARY)


class MemoryMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_chapter_id: str = Field(min_length=1, max_length=64)


class ChapterOrderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=64)
    sort_order: int = Field(ge=0, le=MAX_CHAPTERS)


class ChapterReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: List[ChapterOrderEntry] = Field(
        min_length=1, max_length=MAX_CHAPTERS, description="The full new chapter ordering."
    )

    @field_validator("order")
    @classmethod
    def unique_chapter_ids(cls, value: List[ChapterOrderEntry]) -> List[ChapterOrderEntry]:
        """
        Rejects duplicate chapter ids.

        The API handler compares the request's id set against the memoir's real
        chapter ids; duplicates collapse inside a set() and pass that check
        silently, producing an ordering where one chapter is written twice and
        another never moves. Caught here instead.
        """
        ids = [entry.chapter_id for entry in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Each chapter may appear at most once in the ordering.")
        return value


class ChatMessage(BaseModel):
    """
    One turn of prior conversation, echoed back by the client each request.

    Bounded on both count and per-message length: 40 turns x 4000 chars is ~40k
    tokens re-sent on *every* message, which is a cost amplification problem as
    much as a size one.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(description="Who said this turn.")
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    history: List[ChatMessage] = Field(default_factory=list, max_length=20)


class ProposedAction(BaseModel):
    """
    A single owner-reviewable change an agent wants to make.

    The agent proposes; it never applies. `preview_before` / `preview_after`
    exist so the UI can render a real diff rather than a description of one.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: Literal[
        "move_memory",
        "merge_into_chapter",
        "rename_chapter",
        "rewrite_chapter_summary",
        "reorder_chapters",
        "create_chapter",
        "split_chapter",
    ]
    target_memory_ids: List[str] = Field(default_factory=list, max_length=200)
    target_chapter_id: Optional[str] = Field(default=None, max_length=64)
    preview_before: str = Field(default="", max_length=MAX_CHAPTER_SUMMARY)
    preview_after: str = Field(default="", max_length=MAX_CHAPTER_SUMMARY)
    rationale: str = Field(default="", max_length=1000)


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    reply: str = Field(description="The assistant's reply.")
    proposed_actions: List[ProposedAction] = Field(
        default_factory=list,
        max_length=10,
        description="Owner-reviewable changes. Nothing is applied without confirmation.",
    )


class ApplyProposalRequest(BaseModel):
    """
    Confirms a previously-returned proposal.

    Takes a proposal_id, never a proposal payload. Accepting a structure from a
    request body is precisely how untrusted data reaches the database — the
    reference implementation's `ChapterApplyPayload(chapters: List[Dict[str,
    Any]])` was an unauthenticated cross-tenant write for exactly this reason.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)


class ProposalReviewResponse(BaseModel):
    """
    The owner-facing review payload.

    Carries the plan plus an honest account of what it does and does not cover,
    so the confirm step is a real review. `warnings` is where "3 memories landed
    in Other Memories" and "this replaces 4 AI chapters" surface — if the UI can
    only show a green checkmark, the owner is not actually reviewing anything.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str = Field(description="pending | applied | superseded | expired")
    source: str = Field(description="organize | chat")
    summary_line: str = ""
    plan: ResolvedPlan
    warnings: List[str] = Field(default_factory=list)
    memories_considered: int = Field(ge=0)
    memories_placed: int = Field(ge=0)
    memories_unplaced: int = Field(ge=0)
    # How many existing AI-authored chapters this proposal will delete on apply.
    # Reported so the owner is told what the change costs, not just what it
    # creates. Was previously left at its default of 0, which under-reported
    # every run after the first.
    chapters_replaced: int = Field(
        default=0, ge=0, description="Existing AI-authored chapters this would replace."
    )
    expires_at: Optional[str] = None


class ProposalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    data: Optional[ProposalReviewResponse] = None


class ActionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action_type: str
    actor_user_id: str
    target_ids: List[str] = Field(default_factory=list)
    detail: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class ActionHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    data: List[ActionHistoryEntry] = Field(default_factory=list)