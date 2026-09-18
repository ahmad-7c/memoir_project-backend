"""
@file transcript.py
@description FastAPI router exposing endpoints to request and retry background transcriptions.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from src.schemas.transcript import TranscriptionRequest
from src.domain.transcription_service import (
    enqueue_transcription,
    resolve_transcription_target,
    retry_transcription,
)
from src.core.auth import get_current_user

router = APIRouter(prefix="/api/transcript", tags=["Transcript"])

@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def request_transcription(
    payload: TranscriptionRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Triggers AssemblyAI speech-to-text conversion in the background for a given media asset.
    Requires the caller to own the memoir the media asset belongs to, and the memoir must
    not yet be published.
    """
    user_id = current_user.get("user_id")
    target = resolve_transcription_target(payload.media_asset_id, user_id)

    try:
        enqueue_transcription(
            media_asset_id=payload.media_asset_id,
            memoir_id=target["memoir_id"],
            storage_key=target["storage_key"],
            background_tasks=background_tasks,
        )
        return {"status": "processing", "message": "Transcription task initiated in background."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue transcription task: {str(e)}"
        )


@router.post("/{media_asset_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_transcription_route(
    media_asset_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Owner-only retry for a transcription that failed or stalled. Bounded at 3
    total attempts per asset.
    """
    user_id = current_user.get("user_id")
    return retry_transcription(media_asset_id, user_id, background_tasks)
