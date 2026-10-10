"""
@file domain/narrative_service.py
@description AI-composed biographical narrative: grounded generation, the
validation rules that make its citations trustworthy, and the owner-facing
review/edit/delete/regenerate operations.

THE GOVERNING RULE (see also schemas/narrative.py and the prompt below)

Everything in the narrative must be traceable to a memory the family
supplied. The AI arranges, connects and shapes; it never introduces a fact.
Two rules enforce that mechanically, not just by prompting for it:

1. Every source_memory_id a model returns is checked against the exact set
   of memory ids that was sent for that chapter. An unknown id means the
   model was not grounded, and there is no way to tell which other sections
   in the same run share the same failure -- so the WHOLE generation is
   discarded (NarrativeRejected), never just the offending chapter.
2. Every section must cite at least one memory (enforced by both the Pydantic
   schema and a defensive re-check here, since a best-effort provider's JSON
   mode does not guarantee schema conformance the way Groq's strict mode
   does).

Async note: same shape as organization_service.py. This module's pipeline is
`async def`; `generate_narrative_background` is a thin sync `def` that calls
`asyncio.run(...)`, because an `async def` handed directly to BackgroundTasks
is awaited on the event loop and this pipeline runs for tens of seconds.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

from src.core.config import settings
from src.integrations import narrative_repository as repo
from src.integrations import participant_repository
from src.integrations.llm import LLMCallError, ProviderRejectedOutput, complete_json
from src.schemas.narrative import NarrativeGenerationOutput, NarrativeRejected, NarrativeSectionProposal

logger = logging.getLogger(__name__)

STALL_THRESHOLD_SECONDS = 5 * 60

NARRATIVE_SYSTEM_PROMPT = (
    "You compose flowing biographical prose for a family memoir from memories "
    "the family themselves recorded or wrote. You are writing for a grieving "
    "family: warm, plain, dignified. Never sentimental, never performatively "
    "sad. No flourishes, no purple prose, no invented scene-setting.\n\n"
    "THE GOVERNING RULE -- read this before anything else. Everything you "
    "write must be traceable to the memories given to you. You arrange, "
    "connect and shape; you never introduce a fact.\n"
    "- Every factual statement must come from the memories below. Never add "
    "a person, place, date or event that does not appear in them.\n"
    "- Never invent emotions or motivations. If a memory says someone cried, "
    "you may say so. If none mentions pride, do not write that he was "
    "proud.\n"
    "- Where memories conflict, present both. Never choose a version, never "
    "reconcile them -- you may write 'the family remembers this "
    "differently'.\n"
    "- You may write a bare transition like 'After the bakery years' to join "
    "two sections. You may NOT write a sentence that asserts something no "
    "source memory supports.\n"
    "- If a memory is too thin to build on, quote or closely paraphrase it "
    "rather than embellishing.\n\n"
    "For every section you write, list the exact memory ids (copied exactly "
    "from the ids you were given) it draws from. A section with zero "
    "citations is forbidden -- every section must cite at least one memory "
    "id, and every id you list must be one you were actually given. Never "
    "invent an id.\n\n"
    "Return ONLY a JSON object of EXACTLY this shape, with these EXACT field "
    "names (not synonyms -- a renamed field is treated as a malformed "
    "response and discarded):\n"
    '{"sections": [{"position": 0, "body": "<the prose for this section>", '
    '"source_memory_ids": ["<memory id copied exactly from the input>"]}]}\n'
    "Nothing outside this JSON object."
)


def _memory_payload(memories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Text-only payload sent to the model: id, title, body text, transcript
    text, photo caption, date. Never audio files or images -- only their
    already-transcribed/captioned TEXT, per spec.
    """
    return [
        {
            "memory_id": m["id"],
            "title": m.get("title") or "",
            "body_text": (m.get("body_text") or "")[:4000],
            "transcript": " ".join(m.get("transcript_texts") or [])[:4000],
            "photo_captions": m.get("photo_captions") or [],
            "occurred_start": str(m.get("occurred_start") or ""),
        }
        for m in memories
    ]


def _assert_before_deadline(deadline: float, stage: str) -> None:
    if time.monotonic() > deadline:
        raise LLMCallError(
            f"Narrative generation exceeded its "
            f"{settings.organize_pipeline_deadline_seconds}s budget during {stage}.",
            failoverable=False,
        )


def _validate_sections(
    sections: List[NarrativeSectionProposal], sent_ids: set, context: str
) -> None:
    """
    Rejects the WHOLE generation if any section cites an id outside what was
    sent, or cites nothing. Mirrors organization_service._validate_ids: an
    unknown id is not a formatting problem to patch around, it means the
    response cannot be trusted at all.
    """
    for section in sections:
        if not section.source_memory_ids:
            raise NarrativeRejected(
                f"Model returned a section with zero citations ({context}); "
                "discarding the entire generation."
            )
        for memory_id in section.source_memory_ids:
            if memory_id not in sent_ids:
                raise NarrativeRejected(
                    f"Model returned unknown memory_id={memory_id!r} ({context}); "
                    "discarding the entire generation."
                )


async def _run_chapter_narrative(
    chapter_title: str, memories: List[Dict[str, Any]], deadline: float, *, context: str
) -> List[NarrativeSectionProposal]:
    """One grounded generation call, scoped to exactly the memories given."""
    _assert_before_deadline(deadline, "narrative")

    sent_ids = {m["id"] for m in memories}
    payload = _memory_payload(memories)

    output: NarrativeGenerationOutput = await complete_json(
        system_prompt=NARRATIVE_SYSTEM_PROMPT,
        user_prompt=(
            f"Compose the biographical narrative for the chapter '{chapter_title}', "
            f"using ONLY these {len(payload)} memories (never anything else, never "
            f"information from outside this list):\n{json.dumps(payload)}\n\n"
            "Produce 1 to 6 sections of flowing prose in chronological order. "
            "Each section must list the exact memory ids (copied from above) it "
            "draws from."
        ),
        response_model=NarrativeGenerationOutput,
        purpose=f"narrative[{context}]",
    )

    _validate_sections(output.sections, sent_ids, context)
    return output.sections


async def generate_narrative(memoir_id: str) -> Tuple[List[str], List[str]]:
    """
    Runs the full per-chapter generation, validates everything, and persists
    only if the ENTIRE run is trustworthy. Returns (warnings, skipped_memory_ids).

    Nothing is saved until every chapter's output has been validated --
    see domain docstring on why a partial save is not an option.
    """
    chapters = repo.fetch_chapters_for_memoir(memoir_id)
    if not chapters:
        raise NarrativeRejected(
            "This memoir has no chapters yet. Run organization before generating the narrative."
        )

    deadline = time.monotonic() + settings.organize_pipeline_deadline_seconds

    all_sections: List[Dict[str, Any]] = []
    position = 0
    for chapter in chapters:
        memories = repo.fetch_chapter_memories_for_narrative(memoir_id, chapter["id"])
        if not memories:
            continue

        sections = await _run_chapter_narrative(
            chapter["title"], memories, deadline, context=f"chapter={chapter['id']}"
        )

        for section in sections:
            all_sections.append(
                {
                    "chapter_id": chapter["id"],
                    "position": position,
                    "body": section.body.strip(),
                    "source_memory_ids": section.source_memory_ids,
                }
            )
            position += 1

    if not all_sections:
        raise NarrativeRejected("The model produced no narrative sections for any chapter.")

    sent_memory_ids = repo.fetch_chapter_assigned_memory_ids(memoir_id)
    cited_memory_ids = {mid for s in all_sections for mid in s["source_memory_ids"]}
    skipped = sorted(sent_memory_ids - cited_memory_ids)

    repo.replace_all_sections(memoir_id, all_sections)
    repo.clear_narrative_reviewed(memoir_id)

    warnings: List[str] = []
    if skipped:
        warnings.append(
            f"{len(skipped)} {'memory was' if len(skipped) == 1 else 'memories were'} "
            "not referenced by the narrative. Nothing was deleted -- they're still in "
            "the archive, just not woven into this text."
        )

    return warnings, skipped


def generate_narrative_background(memoir_id: str) -> None:
    """
    Background entrypoint for the whole-memoir generate. Always resolves the
    run to 'ready' or 'failed'. Sync by design -- see module docstring.
    """
    run_id: Optional[str] = None
    try:
        run_id = repo.start_run(memoir_id)
    except Exception:
        logger.exception("could not open narrative generation run (memoir_id=%s)", memoir_id)

    try:
        warnings, skipped = asyncio.run(generate_narrative(memoir_id))
        logger.info(
            "Narrative generation ready for memoir_id=%s (%d memories skipped)",
            memoir_id,
            len(skipped),
        )
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_READY, warnings=warnings)
    except NarrativeRejected as rejected:
        logger.warning("Narrative generation rejected for memoir_id=%s: %s", memoir_id, rejected)
        message = "The AI's narrative couldn't be trusted, so nothing was saved. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except ProviderRejectedOutput as rejected:
        logger.warning("Narrative provider output rejected for memoir_id=%s: %s", memoir_id, rejected)
        message = "The AI's response couldn't be parsed, so nothing was saved. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except LLMCallError as call_err:
        logger.error("Narrative generation provider failure for memoir_id=%s: %s", memoir_id, call_err)
        message = "The AI service is unavailable right now. You can try again in a moment."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except Exception:
        logger.exception("Narrative generation failed for memoir_id=%s", memoir_id)
        message = "Something went wrong while composing the narrative. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)


async def regenerate_section(memoir_id: str, section_id: str) -> None:
    """
    Regenerates ONE section from exactly the memories it already cites --
    not the whole chapter. This keeps "regenerate one section alone" well
    defined: the attribution (which memories) stays fixed, only the AI's
    wording changes.
    """
    section = repo.fetch_narrative_section(section_id, memoir_id)
    source_ids = section.get("source_memory_ids") or []
    if not source_ids:
        raise NarrativeRejected("This section has no source memories to regenerate from.")

    memories = repo.fetch_memories_for_narrative_by_ids(memoir_id, source_ids)
    if not memories:
        raise NarrativeRejected(
            "None of this section's source memories could be found anymore; it cannot be regenerated."
        )

    deadline = time.monotonic() + settings.organize_pipeline_deadline_seconds
    sent_ids = {m["id"] for m in memories}
    payload = _memory_payload(memories)

    output: NarrativeGenerationOutput = await complete_json(
        system_prompt=NARRATIVE_SYSTEM_PROMPT,
        user_prompt=(
            f"Compose exactly ONE section of flowing prose, using ONLY these "
            f"{len(payload)} memories (never anything else):\n{json.dumps(payload)}\n\n"
            "Return exactly one section in the sections array. It must list the "
            "exact memory ids (copied from above) it draws from."
        ),
        response_model=NarrativeGenerationOutput,
        purpose=f"narrative-regenerate[{section_id}]",
    )

    _validate_sections(output.sections, sent_ids, f"section={section_id}")
    if len(output.sections) != 1:
        raise NarrativeRejected(
            f"Expected exactly one regenerated section, got {len(output.sections)}; discarding."
        )

    new_section = output.sections[0]
    repo.replace_section_from_regeneration(
        section_id, memoir_id, new_section.body.strip(), new_section.source_memory_ids
    )
    repo.clear_narrative_reviewed(memoir_id)


def regenerate_section_background(memoir_id: str, section_id: str) -> None:
    """Background entrypoint for a single-section regenerate. Sync by design."""
    run_id: Optional[str] = None
    try:
        run_id = repo.start_run(memoir_id, section_id=section_id)
    except Exception:
        logger.exception(
            "could not open narrative regenerate run (memoir_id=%s section_id=%s)", memoir_id, section_id
        )

    try:
        asyncio.run(regenerate_section(memoir_id, section_id))
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_READY)
    except NarrativeRejected as rejected:
        logger.warning(
            "Section regeneration rejected (memoir_id=%s section_id=%s): %s", memoir_id, section_id, rejected
        )
        message = "The AI's rewrite couldn't be trusted, so this section was left unchanged. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except ProviderRejectedOutput:
        message = "The AI's response couldn't be parsed. This section was left unchanged. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except LLMCallError as call_err:
        logger.error(
            "Section regeneration provider failure (memoir_id=%s section_id=%s): %s",
            memoir_id,
            section_id,
            call_err,
        )
        message = "The AI service is unavailable right now. You can try again in a moment."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Section regeneration failed (memoir_id=%s section_id=%s)", memoir_id, section_id)
        message = "Something went wrong while regenerating this section. You can try again."
        if run_id:
            repo.finish_run(run_id, repo.RUN_STATUS_FAILED, error_message=message)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def compute_effective_status(run: Dict[str, Any]) -> str:
    """Same stall-detection shape as organization_service.compute_effective_organization_status."""
    current_status = run.get("status")
    if current_status != repo.RUN_STATUS_PROCESSING:
        return current_status or "none"

    started_raw = run.get("started_at")
    if not started_raw:
        return current_status

    try:
        started_at = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
    except ValueError:
        return current_status

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return "stalled" if age_seconds > STALL_THRESHOLD_SECONDS else current_status


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def verify_owner_access(memoir_id: str, user_id: str) -> None:
    """Generation and editing are owner-only. 404, not 403 -- see root AGENTS.md."""
    participant_res = participant_repository.fetch_participant(memoir_id, user_id)
    participants = participant_res.data or []
    if not participants or participants[0].get("role") != "owner":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")
