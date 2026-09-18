"""
@file storage_adapter.py
@description Infrastructure adapter service managing low-level object storage 
interactions, file validations, signed upload/playback URLs, and bucket cleanup.
The API server never handles raw file bytes directly; this module handles secure 
direct-to-cloud communication.
"""

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from supabase import create_client

from src.core.config import settings

logger = logging.getLogger(__name__)

# Initialized once at import using the service_role key to bypass RLS policies.
# Security Warning: This privileged client must never be exposed or imported in browser code.
_client = create_client(settings.supabase_url, settings.supabase_secret_key)

# Strict allowlist mapping permitted MIME types to internal media types and extensions.
ALLOWED_MIME = {
    "audio/webm": ("audio", "webm"),
    "audio/ogg":  ("audio", "ogg"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp4":  ("audio", "m4a"),
    "audio/wav":  ("audio", "wav"),
    "image/jpeg": ("image", "jpg"),
    "image/png":  ("image", "png"),
    "image/webp": ("image", "webp"),
    "image/heic": ("image", "heic"),
}


@dataclass
class SignedUpload:
    """
    Data container representing a secure signed upload session parameters 
    returned to the client for direct bucket uploads.
    """
    path: str
    signed_url: str
    token: str


def validate_upload(mime_type: str) -> tuple[str, str]:
    """
    Validates file MIME types against the permitted allowlist 
    before any bytes are transferred.

    Args:
        mime_type (str): The MIME type of the incoming file.

    Returns:
        tuple[str, str]: A tuple containing the mapped media type and file extension.
    """
    if mime_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="That file type isn't supported. Try a photo or a voice recording.",
        )

    return ALLOWED_MIME[mime_type]


def build_key(memoir_id: uuid.UUID, media_type: str, extension: str) -> str:
    """
    Generates a secure, collision-free server path for object storage.
    Note: Strips out unsafe or malicious user filenames to prevent path traversal issues.

    Args:
        memoir_id (uuid.UUID): The unique memoir identifier container.
        media_type (str): The classification of the media (e.g., 'audio' or 'image').
        extension (str): The verified file extension.

    Returns:
        str: The structured storage file path key.
    """
    return f"memoirs/{memoir_id}/{media_type}/{uuid.uuid4()}.{extension}"


def create_signed_upload(key: str) -> SignedUpload:
    """
    Requests a short-lived signed upload URL from Supabase storage 
    to enable secure direct-to-cloud client file uploads.

    Args:
        key (str): The target storage path key.

    Returns:
        SignedUpload: A data object containing the path, signed URL, and auth token.

    Raises:
        HTTPException (503): If the storage provider fails to generate the upload URL.
    """
    try:
        res = _client.storage.from_(settings.supabase_media_bucket).create_signed_upload_url(key)
    except Exception as exc:
        logger.error("Signed upload URL failed key=%s: %s", key, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We couldn't start the upload just now. Please try again in a moment.",
        )
    return SignedUpload(
        path=res.get("path", key),
        signed_url=res.get("signed_url") or res.get("signedURL"),
        token=res["token"],
    )


def object_exists(key: str) -> int | None:
    """
    Queries cloud storage directly to confirm whether a file physically exists.
    Acts as the source of truth rather than trusting browser completion claims.

    Args:
        key (str): The storage path key to check.

    Returns:
        int | None: The file size in bytes if found, otherwise None.
    """
    folder, _, filename = key.rpartition("/")
    try:
        items = _client.storage.from_(settings.supabase_media_bucket).list(
            folder, {"search": filename}
        )
    except Exception as exc:
        logger.warning("Existence check failed key=%s: %s", key, exc)
        return None

    for item in items or []:
        if item.get("name") == filename:
            meta = item.get("metadata") or {}
            return meta.get("size")
    return None


def create_playback_url(key: str, ttl_seconds: int | None = None) -> str | None:
    """
    Generates a fresh, expiring temporary signed URL for media streaming/playback.
    Note: These URLs are dynamically generated per response and never stored in the database.

    Args:
        key (str): The storage path key of the media asset.
        ttl_seconds (int | None): Override for the signed URL lifetime. Defaults
            to settings.media_signed_url_ttl — callers with a longer-running use
            (e.g. PDF export, which must still be able to fetch the image by the
            time rendering happens) can pass a longer TTL explicitly.

    Returns:
        str | None: The temporary playback URL string, or None if generation fails.
    """
    try:
        res = _client.storage.from_(settings.supabase_media_bucket).create_signed_url(
            key, ttl_seconds if ttl_seconds is not None else settings.media_signed_url_ttl
        )
        return res.get("signedURL") or res.get("signed_url")
    except Exception as exc:
        logger.warning("Playback URL failed key=%s: %s", key, exc)
        return None


def remove_object(key: str) -> None:
    """
    Deletes an object permanently from the storage bucket. 
    Utilized during manual asset deletion and automated orphan sweep jobs.

    Args:
        key (str): The storage path key to delete.
    """
    try:
        _client.storage.from_(settings.supabase_media_bucket).remove([key])
    except Exception as exc:
        logger.warning("Object delete failed key=%s: %s", key, exc)