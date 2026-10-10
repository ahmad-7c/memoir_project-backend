"""
@file integrations/narrative_repository.py
@description Data access for the AI-composed biographical narrative: fetching
per-chapter memory text for generation, persisting validated sections and
their source citations, run-status tracking, and the full-memory "Sources"
expand.

Every mutation takes `memoir_id` and scopes by it, same rule as
organization_repository.py and for the same reason: `supabase_admin` bypasses
RLS, so the filter is the only thing between a bug and a cross-tenant write.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import HTTPException, status

from src.integrations import storage_adapter
from src.integrations.supabase_client import supabase_admin

logger = logging.getLogger(__name__)

RUN_STATUS_PROCESSING = "processing"
RUN_STATUS_READY = "ready"
RUN_STATUS_FAILED = "failed"

_IN_FILTER_CHUNK = 100


def _chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Input: what gets sent to the model
# ---------------------------------------------------------------------------


def fetch_chapters_for_memoir(memoir_id: str) -> List[Dict[str, Any]]:
    res = (
        supabase_admin.table("chapter")
        .select("id, title, sort_order")
        .eq("memoir_id", memoir_id)
        .order("sort_order")
        .execute()
    )
    return res.data or []


def fetch_chapter_memories_for_narrative(memoir_id: str, chapter_id: str) -> List[Dict[str, Any]]:
    """
    Text-only payload for one chapter's memories: id, title, body text,
    transcript text, photo captions, date. NEVER audio files or images --
    only their already-transcribed/captioned TEXT, per spec.
    """
    memories_res = (
        supabase_admin.table("memory")
        .select("id, title, body_text, occurred_start")
        .eq("memoir_id", memoir_id)
        .eq("chapter_id", chapter_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return _enrich_memories_with_text(memoir_id, memories_res.data or [])


def fetch_memories_for_narrative_by_ids(memoir_id: str, memory_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Same text-only payload as fetch_chapter_memories_for_narrative, but for an
    explicit id list rather than a whole chapter -- used to regenerate one
    section from exactly the memories it already cites.
    """
    if not memory_ids:
        return []
    memories_res = (
        supabase_admin.table("memory")
        .select("id, title, body_text, occurred_start")
        .eq("memoir_id", memoir_id)
        .in_("id", memory_ids)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .execute()
    )
    return _enrich_memories_with_text(memoir_id, memories_res.data or [])


def _enrich_memories_with_text(memoir_id: str, memories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Attaches each memory's transcript text and photo captions. Each memory's
    media is resolved in follow-up queries rather than a PostgREST embedding
    join, matching this codebase's existing convention
    (organization_repository.fetch_owner_locked_memory_ids).
    """
    if not memories:
        return []

    memory_ids = [m["id"] for m in memories]

    links_res = (
        supabase_admin.table("memory_media")
        .select("memory_id, media_asset_id")
        .eq("memoir_id", memoir_id)
        .in_("memory_id", memory_ids)
        .execute()
    )
    links = links_res.data or []
    asset_ids = sorted({l["media_asset_id"] for l in links})

    assets_by_id: Dict[str, Dict[str, Any]] = {}
    if asset_ids:
        for chunk in _chunked(asset_ids, _IN_FILTER_CHUNK):
            assets_res = (
                supabase_admin.table("media_asset")
                .select("id, kind, caption")
                .in_("id", chunk)
                .execute()
            )
            for a in assets_res.data or []:
                assets_by_id[a["id"]] = a

    audio_asset_ids = [aid for aid, a in assets_by_id.items() if a.get("kind") == "audio"]
    transcripts_by_asset_id: Dict[str, str] = {}
    if audio_asset_ids:
        for chunk in _chunked(audio_asset_ids, _IN_FILTER_CHUNK):
            t_res = (
                supabase_admin.table("transcript")
                .select("media_asset_id, display_text")
                .in_("media_asset_id", chunk)
                .execute()
            )
            for row in t_res.data or []:
                if row.get("display_text"):
                    transcripts_by_asset_id[row["media_asset_id"]] = row["display_text"]

    media_by_memory: Dict[str, List[Dict[str, Any]]] = {}
    for link in links:
        asset = assets_by_id.get(link["media_asset_id"])
        if asset:
            media_by_memory.setdefault(link["memory_id"], []).append(asset)

    enriched: List[Dict[str, Any]] = []
    for memory in memories:
        assets = media_by_memory.get(memory["id"], [])
        photo_captions = [a["caption"] for a in assets if a.get("kind") == "photo" and a.get("caption")]
        transcript_texts = [
            transcripts_by_asset_id[a["id"]] for a in assets if a.get("kind") == "audio" and a["id"] in transcripts_by_asset_id
        ]
        enriched.append(
            {
                "id": memory["id"],
                "title": memory.get("title") or "",
                "body_text": memory.get("body_text") or "",
                "occurred_start": memory.get("occurred_start"),
                "transcript_texts": transcript_texts,
                "photo_captions": photo_captions,
            }
        )
    return enriched


def fetch_chapter_assigned_memory_ids(memoir_id: str) -> Set[str]:
    """
    Every submitted, non-deleted memory that belongs to some chapter -- the
    full set a whole-memoir generation run sends to the model across all
    chapters. Used to compute which memories ended up cited by zero sections.
    """
    res = (
        supabase_admin.table("memory")
        .select("id")
        .eq("memoir_id", memoir_id)
        .eq("status", "submitted")
        .is_("deleted_at", "null")
        .not_.is_("chapter_id", "null")
        .execute()
    )
    return {row["id"] for row in (res.data or [])}


# ---------------------------------------------------------------------------
# Persisting sections
# ---------------------------------------------------------------------------


def replace_all_sections(
    memoir_id: str, sections: List[Dict[str, Any]]
) -> None:
    """
    Replaces every narrative_section (and, via cascade, narrative_source) row
    for this memoir with a freshly validated set.

    Safe to do as delete-then-insert (no compensating rollback) because
    generation is owner-only and gated on the memoir NOT being published --
    no reader can ever observe the brief window with zero sections, since
    readers only reach this memoir after it's published, by which point
    generation already finished.

    Each `sections` entry: {chapter_id, position, body, source_memory_ids}.
    """
    supabase_admin.table("narrative_section").delete().eq("memoir_id", memoir_id).execute()

    if not sections:
        return

    section_rows = []
    for s in sections:
        section_rows.append(
            {
                "memoir_id": memoir_id,
                "chapter_id": s["chapter_id"],
                "position": s["position"],
                "body": s["body"],
                "body_original": s["body"],
                "owner_edited": False,
            }
        )

    inserted = supabase_admin.table("narrative_section").insert(section_rows).execute()
    if not inserted.data or len(inserted.data) != len(section_rows):
        raise RuntimeError("Failed to insert narrative_section rows.")

    source_rows = []
    for row, original in zip(inserted.data, sections):
        for memory_id in original["source_memory_ids"]:
            source_rows.append({"narrative_section_id": row["id"], "memory_id": memory_id})

    if source_rows:
        for chunk in _chunked(source_rows, 500):
            supabase_admin.table("narrative_source").insert(chunk).execute()


def fetch_narrative_sections(memoir_id: str) -> List[Dict[str, Any]]:
    sections_res = (
        supabase_admin.table("narrative_section")
        .select("id, memoir_id, chapter_id, position, body, body_original, owner_edited, created_at, updated_at")
        .eq("memoir_id", memoir_id)
        .order("position")
        .execute()
    )
    sections = sections_res.data or []
    if not sections:
        return []

    section_ids = [s["id"] for s in sections]
    sources_by_section = _fetch_sources_for_sections(section_ids)
    for s in sections:
        s["source_memory_ids"] = sources_by_section.get(s["id"], [])
    return sections


def fetch_narrative_section(section_id: str, memoir_id: str) -> Dict[str, Any]:
    res = (
        supabase_admin.table("narrative_section")
        .select("id, memoir_id, chapter_id, position, body, body_original, owner_edited, created_at, updated_at")
        .eq("id", section_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Narrative section not found.")
    section = res.data[0]
    section["source_memory_ids"] = _fetch_sources_for_sections([section_id]).get(section_id, [])
    return section


def _fetch_sources_for_sections(section_ids: List[str]) -> Dict[str, List[str]]:
    if not section_ids:
        return {}
    by_section: Dict[str, List[str]] = {sid: [] for sid in section_ids}
    for chunk in _chunked(section_ids, _IN_FILTER_CHUNK):
        res = (
            supabase_admin.table("narrative_source")
            .select("narrative_section_id, memory_id")
            .in_("narrative_section_id", chunk)
            .execute()
        )
        for row in res.data or []:
            by_section.setdefault(row["narrative_section_id"], []).append(row["memory_id"])
    return by_section


def update_section_body(section_id: str, memoir_id: str, body: str) -> Dict[str, Any]:
    """Owner edit. `body_original` is never touched -- see the module docstring."""
    res = (
        supabase_admin.table("narrative_section")
        .update({"body": body, "owner_edited": True, "updated_at": _now()})
        .eq("id", section_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Narrative section not found.")
    return fetch_narrative_section(section_id, memoir_id)


def delete_section(section_id: str, memoir_id: str) -> None:
    res = (
        supabase_admin.table("narrative_section")
        .delete()
        .eq("id", section_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Narrative section not found.")


def replace_section_from_regeneration(
    section_id: str, memoir_id: str, body: str, source_memory_ids: List[str]
) -> Dict[str, Any]:
    """
    Overwrites one section with a fresh, independently-validated AI output.
    Both `body` and `body_original` are replaced -- this IS a new AI output,
    not an owner edit, so `owner_edited` resets to False and the old
    `body_original` is correctly discarded (it described the previous
    generation, not this one).
    """
    res = (
        supabase_admin.table("narrative_section")
        .update({"body": body, "body_original": body, "owner_edited": False, "updated_at": _now()})
        .eq("id", section_id)
        .eq("memoir_id", memoir_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Narrative section not found.")

    supabase_admin.table("narrative_source").delete().eq("narrative_section_id", section_id).execute()
    if source_memory_ids:
        supabase_admin.table("narrative_source").insert(
            [{"narrative_section_id": section_id, "memory_id": mid} for mid in source_memory_ids]
        ).execute()

    return fetch_narrative_section(section_id, memoir_id)


def narrative_sections_exist(memoir_id: str) -> bool:
    res = (
        supabase_admin.table("narrative_section")
        .select("id", count="exact")
        .eq("memoir_id", memoir_id)
        .limit(1)
        .execute()
    )
    return bool(res.count)


# ---------------------------------------------------------------------------
# Owner review gate
# ---------------------------------------------------------------------------


def mark_narrative_reviewed(memoir_id: str) -> str:
    now = _now()
    supabase_admin.table("memoir").update({"narrative_reviewed_at": now}).eq("id", memoir_id).execute()
    return now


def clear_narrative_reviewed(memoir_id: str) -> None:
    """
    Any change to the narrative's content invalidates a prior review -- a
    review is a claim about having read THIS text, and regenerating (whole or
    single-section) produces different text.
    """
    supabase_admin.table("memoir").update({"narrative_reviewed_at": None}).eq("id", memoir_id).execute()


# ---------------------------------------------------------------------------
# Generation run tracking
# ---------------------------------------------------------------------------


def start_run(memoir_id: str, section_id: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {
        "memoir_id": memoir_id,
        "status": RUN_STATUS_PROCESSING,
        "started_at": _now(),
    }
    if section_id:
        payload["section_id"] = section_id
    res = supabase_admin.table("narrative_generation_run").insert(payload).execute()
    if not res.data:
        raise RuntimeError("Failed to open narrative generation run.")
    return res.data[0]["id"]


def finish_run(
    run_id: str,
    run_status: str,
    *,
    error_message: Optional[str] = None,
    warnings: Optional[List[str]] = None,
) -> None:
    payload: Dict[str, Any] = {"status": run_status, "completed_at": _now()}
    if error_message is not None:
        payload["error_message"] = error_message[:500]
    if warnings is not None:
        payload["warnings"] = warnings
    try:
        supabase_admin.table("narrative_generation_run").update(payload).eq("id", run_id).eq(
            "status", RUN_STATUS_PROCESSING
        ).execute()
    except Exception:
        logger.exception("failed to close narrative generation run id=%s", run_id)


def fetch_latest_run(memoir_id: str) -> Optional[Dict[str, Any]]:
    res = (
        supabase_admin.table("narrative_generation_run")
        .select("id, section_id, status, error_message, warnings, started_at, completed_at")
        .eq("memoir_id", memoir_id)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


# ---------------------------------------------------------------------------
# The Sources expand -- full, verbatim source memories
# ---------------------------------------------------------------------------


def fetch_sections_for_export(memoir_id: str) -> List[Dict[str, Any]]:
    """
    Narrative sections plus a short, human-readable attribution line per
    section for the PDF -- "From Tom's recording and Sara's written memory"
    -- WITHOUT the full source memory text. The PDF must be able to say which
    contributors a section drew from without reproducing their words; the
    full originals stay on the memoir's web page via the Sources button.
    """
    sections = fetch_narrative_sections(memoir_id)
    if not sections:
        return []

    all_memory_ids = sorted({mid for s in sections for mid in s["source_memory_ids"]})
    if not all_memory_ids:
        return [{**s, "attribution_line": ""} for s in sections]

    memories_by_id: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunked(all_memory_ids, _IN_FILTER_CHUNK):
        res = (
            supabase_admin.table("memory")
            .select("id, author_participant_id")
            .eq("memoir_id", memoir_id)
            .in_("id", chunk)
            .execute()
        )
        for row in res.data or []:
            memories_by_id[row["id"]] = row

    participant_ids = sorted(
        {m["author_participant_id"] for m in memories_by_id.values() if m.get("author_participant_id")}
    )
    names_by_participant: Dict[str, str] = {}
    if participant_ids:
        for chunk in _chunked(participant_ids, _IN_FILTER_CHUNK):
            res = (
                supabase_admin.table("memoir_participant")
                .select("id, display_name")
                .in_("id", chunk)
                .execute()
            )
            for row in res.data or []:
                names_by_participant[row["id"]] = row.get("display_name")

    links_res = (
        supabase_admin.table("memory_media")
        .select("memory_id, media_asset_id")
        .eq("memoir_id", memoir_id)
        .in_("memory_id", all_memory_ids)
        .execute()
    )
    links = links_res.data or []
    asset_ids = sorted({l["media_asset_id"] for l in links})

    kind_by_asset: Dict[str, str] = {}
    if asset_ids:
        for chunk in _chunked(asset_ids, _IN_FILTER_CHUNK):
            res = supabase_admin.table("media_asset").select("id, kind").in_("id", chunk).execute()
            for a in res.data or []:
                kind_by_asset[a["id"]] = a.get("kind")

    # Highest-priority media kind per memory: a recording is the most
    # personal attribution, then a photograph, then "written memory" for a
    # text-only entry.
    _KIND_PRIORITY = {"audio": 0, "photo": 1, "video": 1}
    _KIND_LABEL = {"audio": "recording", "photo": "photograph", "video": "video"}

    kind_by_memory: Dict[str, str] = {}
    for link in links:
        kind = kind_by_asset.get(link["media_asset_id"])
        if not kind:
            continue
        current = kind_by_memory.get(link["memory_id"])
        if current is None or _KIND_PRIORITY.get(kind, 2) < _KIND_PRIORITY.get(current, 2):
            kind_by_memory[link["memory_id"]] = kind

    for section in sections:
        # contributor display_name -> best (lowest-priority-number) kind
        best_kind_by_contributor: Dict[str, str] = {}
        for memory_id in section["source_memory_ids"]:
            memory = memories_by_id.get(memory_id)
            if not memory:
                continue
            name = names_by_participant.get(memory.get("author_participant_id"))
            if not name:
                continue
            kind = kind_by_memory.get(memory_id, "text")
            current = best_kind_by_contributor.get(name)
            if current is None or _KIND_PRIORITY.get(kind, 2) < _KIND_PRIORITY.get(current, 2):
                best_kind_by_contributor[name] = kind

        phrases = [
            f"{name}'s {_KIND_LABEL.get(kind, 'written memory')}"
            for name, kind in sorted(best_kind_by_contributor.items())
        ]
        section["attribution_line"] = _join_with_and(phrases)

    return sections


def _join_with_and(phrases: List[str]) -> str:
    if not phrases:
        return ""
    if len(phrases) == 1:
        return f"From {phrases[0]}."
    if len(phrases) == 2:
        return f"From {phrases[0]} and {phrases[1]}."
    return f"From {', '.join(phrases[:-1])}, and {phrases[-1]}."


def fetch_source_memories(memoir_id: str, memory_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Full verbatim content for the memories a narrative section cites: text,
    photos (signed URL, never a raw storage path), audio (signed URL +
    transcript), and the contributor's display name. Same allowlist
    discipline as the share-link reader view -- no storage_key, no internal
    participant id.
    """
    if not memory_ids:
        return []

    memories_by_id: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunked(memory_ids, _IN_FILTER_CHUNK):
        res = (
            supabase_admin.table("memory")
            .select("id, title, body_text, occurred_start, author_participant_id")
            .eq("memoir_id", memoir_id)
            .in_("id", chunk)
            .execute()
        )
        for row in res.data or []:
            memories_by_id[row["id"]] = row

    if not memories_by_id:
        return []

    participant_ids = sorted({m["author_participant_id"] for m in memories_by_id.values() if m.get("author_participant_id")})
    names_by_participant: Dict[str, str] = {}
    if participant_ids:
        for chunk in _chunked(participant_ids, _IN_FILTER_CHUNK):
            res = (
                supabase_admin.table("memoir_participant")
                .select("id, display_name")
                .in_("id", chunk)
                .execute()
            )
            for row in res.data or []:
                names_by_participant[row["id"]] = row.get("display_name")

    links_res = (
        supabase_admin.table("memory_media")
        .select("memory_id, media_asset_id")
        .eq("memoir_id", memoir_id)
        .in_("memory_id", list(memories_by_id.keys()))
        .execute()
    )
    links = links_res.data or []
    asset_ids = sorted({l["media_asset_id"] for l in links})

    assets_by_id: Dict[str, Dict[str, Any]] = {}
    if asset_ids:
        for chunk in _chunked(asset_ids, _IN_FILTER_CHUNK):
            res = (
                supabase_admin.table("media_asset")
                .select("id, kind, mime_type, caption, duration_ms, width_px, height_px, storage_key")
                .in_("id", chunk)
                .execute()
            )
            for a in res.data or []:
                assets_by_id[a["id"]] = a

    audio_asset_ids = [aid for aid, a in assets_by_id.items() if a.get("kind") == "audio"]
    transcripts_by_asset_id: Dict[str, str] = {}
    if audio_asset_ids:
        for chunk in _chunked(audio_asset_ids, _IN_FILTER_CHUNK):
            res = (
                supabase_admin.table("transcript")
                .select("media_asset_id, display_text")
                .in_("media_asset_id", chunk)
                .execute()
            )
            for row in res.data or []:
                transcripts_by_asset_id[row["media_asset_id"]] = row.get("display_text")

    media_by_memory: Dict[str, List[str]] = {}
    for link in links:
        media_by_memory.setdefault(link["memory_id"], []).append(link["media_asset_id"])

    results: List[Dict[str, Any]] = []
    for memory_id in memory_ids:
        memory = memories_by_id.get(memory_id)
        if not memory:
            continue
        media_list = []
        for asset_id in media_by_memory.get(memory_id, []):
            asset = assets_by_id.get(asset_id)
            if not asset:
                continue
            storage_key = asset.get("storage_key")
            media_list.append(
                {
                    "id": asset["id"],
                    "kind": asset.get("kind"),
                    "mime_type": asset.get("mime_type"),
                    "caption": asset.get("caption"),
                    "duration_ms": asset.get("duration_ms"),
                    "width_px": asset.get("width_px"),
                    "height_px": asset.get("height_px"),
                    "playback_url": storage_adapter.create_playback_url(storage_key) if storage_key else None,
                    "transcript_text": transcripts_by_asset_id.get(asset_id) if asset.get("kind") == "audio" else None,
                }
            )
        results.append(
            {
                "id": memory["id"],
                "title": memory.get("title"),
                "body_text": memory.get("body_text"),
                "occurred_start": memory.get("occurred_start"),
                "author_name": names_by_participant.get(memory.get("author_participant_id")),
                "media": media_list,
            }
        )
    return results
