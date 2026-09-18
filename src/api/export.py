"""
@file api/export.py
@description API router for triggering and monitoring memoir PDF exports.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from src.domain.export_service import ExportService
from src.core.auth import get_current_user
from src.integrations.export_repository import ExportRepository


router = APIRouter(prefix="/api/memoirs", tags=["Export"])

@router.post("/{memoir_id}/export", status_code=status.HTTP_202_ACCEPTED)
def request_memoir_export(
    memoir_id: str,
    background_tasks: BackgroundTasks,
    current_user_id: str = Depends(get_current_user)
):
    """
    Initiates an asynchronous PDF export job for the specified memoir.
    Returns 202 Accepted immediately with job status 'queued'.
    """
    job_info = ExportService.initiate_export(memoir_id, current_user_id)
    
    # Queue background generation task to prevent gateway timeouts
    background_tasks.add_task(
        ExportService.process_export_background,
        export_id=job_info["export_id"],
        memoir_id=memoir_id
    )

    return {
        "success": True,
        "data": job_info
    }
    
@router.get("/{memoir_id}/export/latest")
def get_latest_export_status(
    memoir_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Fetches the latest export job status and signed download URL if ready. Owner-only."""
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    ExportService.verify_owner_access(memoir_id, user_id)

    job = ExportRepository.get_latest_export(memoir_id)
    if not job:
        return {"status": "none"}
    
    download_url = None
    if job["status"] == "ready" and job.get("storage_key"):
        download_url = ExportRepository.get_signed_download_url(job["storage_key"])

    return {
        "success": True,
        "status": job["status"],
        "error_message": job.get("error_message"),
        "download_url": download_url
    }