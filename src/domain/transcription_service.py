"""
@file domain/transcription_service.py
@description Background audio transcription lifecycle: queue, run, retry, and
report status. Runs as a FastAPI BackgroundTasks job (see report for why not
Celery), so every entry point here is written to resolve to 'ready' or 'failed'
on its own — nothing upstream is waiting on a return value.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import assemblyai as aai
from fastapi import HTTPException, status

from src.core.config import (
    TRANSCRIPTION_STATUS_FAILED,
    TRANSCRIPTION_STATUS_PROCESSING,
    TRANSCRIPTION_STATUS_READY,
    settings,
)
from src.domain.authorization import assert_memoir_editable
from src.integrations import participant_repository
from src.integrations.memory_repository import (
    fetch_media_asset_record,
    update_media_asset_transcription_status,
    upsert_transcript_record,
)
from src.integrations.supabase_client import supabase_admin

logger = logging.getLogger(__name__)

MAX_TRANSCRIPTION_ATTEMPTS = 3
STALL_THRESHOLD_SECONDS = 5 * 60

STALLED = "stalled"  # presentation-only value; never written to the DB enum


def _fetch_transcript(media_asset_id: str) -> Optional[dict]:
    res = supabase_admin.table("transcript").select("*").eq("media_asset_id", media_asset_id).maybe_single().execute()
    return res.data if res else None


def _save_transcript(media_asset_id: str, memoir_id: str, existing: Optional[dict], **fields) -> dict:
    """
    Upserts the transcript row. raw_text/engine are NOT NULL with no default, so
    every call carries them forward from the existing row (or a safe placeholder
    for a brand-new row) even when this particular write doesn't concern them.
    edited_text is never touched here — that's the owner's manual correction and
    no automated path may overwrite it.
    """
    existing = existing or {}
    payload = {
        "media_asset_id": media_asset_id,
        "memoir_id": memoir_id,
        "raw_text": existing.get("raw_text") or "",
        "engine": existing.get("engine") or "assemblyai",
        **fields,
    }
    return upsert_transcript_record(payload).data


def compute_effective_transcription_status(asset: dict) -> str:
    """
    Returns the status to show a caller, overriding a stuck 'processing' with
    'stalled' once it has run longer than STALL_THRESHOLD_SECONDS.
    BackgroundTasks has no crash recovery or heartbeat, so a job that died with
    the server process would otherwise show as 'processing' forever — this is
    what surfaces it (with a retry option) instead.

    Expects `asset` to be a media_asset dict optionally carrying a nested
    `transcript` dict (as the memory feed hydration already attaches).
    """
    current_status = asset.get("transcription_status")
    if current_status != TRANSCRIPTION_STATUS_PROCESSING:
        return current_status

    transcript = asset.get("transcript") or {}
    timestamp_raw = transcript.get("updated_at") or transcript.get("created_at")
    if not timestamp_raw:
        return current_status

    try:
        started_at = datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
    except ValueError:
        return current_status

    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return STALLED if age_seconds > STALL_THRESHOLD_SECONDS else current_status


def resolve_transcription_target(media_asset_id: str, user_id: str) -> dict:
    """
    Resolves the memoir_id and storage_key for a transcription request purely from
    the media_asset row, and verifies the caller is an active participant of the
    memoir that asset belongs to. Never trusts a client-supplied memoir_id or
    storage_key. Returns 404 (not 403) on any mismatch so ownership of a given
    media_asset_id can't be probed.
    """
    asset = fetch_media_asset_record(media_asset_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found.")

    memoir_id = asset.get("memoir_id")
    storage_key = asset.get("storage_key")

    participant_res = participant_repository.fetch_participant(memoir_id, user_id)
    if not participant_res.data or not storage_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found.")

    assert_memoir_editable(memoir_id)

    return {"memoir_id": memoir_id, "storage_key": storage_key}


def enqueue_transcription(media_asset_id: str, memoir_id: str, storage_key: str, background_tasks) -> None:
    """
    Queues transcription in the background. Call only after the caller's own DB
    writes have committed — this schedules work for after the response is sent,
    never before. Idempotent: a no-op if this asset already transcribed
    successfully.
    """
    asset = fetch_media_asset_record(media_asset_id)
    if asset and asset.get("transcription_status") == TRANSCRIPTION_STATUS_READY:
        return

    background_tasks.add_task(
        transcribe_and_store_audio,
        media_asset_id=media_asset_id,
        memoir_id=memoir_id,
        storage_key=storage_key,
    )


def retry_transcription(media_asset_id: str, user_id: str, background_tasks) -> dict:
    """
    Owner-initiated retry. Only allowed when the asset is 'failed' or 'stalled'
    (a 'processing' job stuck past the stall threshold), and bounded at
    MAX_TRANSCRIPTION_ATTEMPTS total attempts.
    """
    asset = fetch_media_asset_record(media_asset_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found.")

    memoir_id = asset.get("memoir_id")
    storage_key = asset.get("storage_key")

    participant_res = participant_repository.fetch_participant(memoir_id, user_id)
    if not participant_res.data or not storage_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found.")

    assert_memoir_editable(memoir_id)

    existing = _fetch_transcript(media_asset_id)
    effective_status = compute_effective_transcription_status({**asset, "transcript": existing})
    if effective_status not in (TRANSCRIPTION_STATUS_FAILED, STALLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This recording isn't in a state that can be retried."
        )

    attempt_count = (existing or {}).get("attempt_count") or 0
    if attempt_count >= MAX_TRANSCRIPTION_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Maximum retry attempts reached for this recording."
        )

    background_tasks.add_task(
        transcribe_and_store_audio,
        media_asset_id=media_asset_id,
        memoir_id=memoir_id,
        storage_key=storage_key,
    )
    return {"status": "processing", "message": "Retry queued."}


def transcribe_and_store_audio(media_asset_id: str, memoir_id: str, storage_key: str) -> None:
    """
    Downloads audio from storage, sends it to AssemblyAI, and persists the
    result. Runs as a background task: never raises back to a caller, always
    resolves the asset to 'ready' or 'failed'. Safe to invoke more than once for
    the same asset — a no-op if already successfully transcribed, and gives up
    without retrying past MAX_TRANSCRIPTION_ATTEMPTS.
    """
    asset = fetch_media_asset_record(media_asset_id)
    if asset and asset.get("transcription_status") == TRANSCRIPTION_STATUS_READY:
        return

    existing = _fetch_transcript(media_asset_id)
    attempt_count = (existing or {}).get("attempt_count") or 0
    if attempt_count >= MAX_TRANSCRIPTION_ATTEMPTS:
        logger.warning("Refusing to transcribe media_asset_id=%s: attempt cap reached.", media_asset_id)
        return

    now = datetime.now(timezone.utc).isoformat()
    _save_transcript(
        media_asset_id, memoir_id, existing,
        attempt_count=attempt_count + 1, error_message=None, updated_at=now,
    )
    update_media_asset_transcription_status(media_asset_id, TRANSCRIPTION_STATUS_PROCESSING)

    # No missing-key check here: settings.assemblyai_api_key has no default,
    # so the app refuses to start at all without it (see src/core/config.py).
    aai.settings.api_key = settings.assemblyai_api_key
    transcriber = aai.Transcriber()

    try:
        audio_bytes = None
        for attempt in range(1, 4):
            try:
                audio_bytes = supabase_admin.storage.from_(settings.supabase_media_bucket).download(storage_key)
                if audio_bytes:
                    break
            except Exception as dl_err:
                logger.warning("Download attempt %d failed for key %s: %s", attempt, storage_key, dl_err)
                if attempt < 3:
                    time.sleep(1.5)

        if not audio_bytes:
            raise RuntimeError(f"Download returned empty bytes or 404 after retries for key: {storage_key}")

        upload_url = transcriber.upload_file(audio_bytes)
        transcript_result = transcriber.transcribe(upload_url)

        if transcript_result.status == aai.TranscriptStatus.error:
            raise RuntimeError(transcript_result.error or "AssemblyAI returned an error status.")

        raw_text = transcript_result.text or ""
        has_owner_edit = bool((existing or {}).get("edited_text"))

        _save_transcript(
            media_asset_id, memoir_id, existing,
            raw_text=raw_text,
            # Never overwrite an owner's manual correction with a fresh ASR pass.
            display_text=(existing.get("edited_text") if has_owner_edit else raw_text),
            engine="assemblyai",
            provider_job_id=getattr(transcript_result, "id", None),
            confidence=transcript_result.confidence,
            language=transcript_result.language_code or "en",
            error_message=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        update_media_asset_transcription_status(media_asset_id, TRANSCRIPTION_STATUS_READY)

    except Exception as exc:
        logger.exception("Transcription failed for media_asset_id=%s", media_asset_id)
        _mark_failed(media_asset_id, memoir_id, existing, "We couldn't transcribe this recording. You can try again.")


def _mark_failed(media_asset_id: str, memoir_id: str, existing: Optional[dict], message: str) -> None:
    _save_transcript(
        media_asset_id, memoir_id, existing,
        error_message=message, updated_at=datetime.now(timezone.utc).isoformat(),
    )
    update_media_asset_transcription_status(media_asset_id, TRANSCRIPTION_STATUS_FAILED)
