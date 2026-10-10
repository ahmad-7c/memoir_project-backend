"""
@file memoir_service.py
@description Business logic and orchestration service for creating memoir containers, 
normalizing subject dates, and automatically registering the creator as the owner participant,
fully decoupled from direct database infrastructure calls.
"""

import secrets

from fastapi import HTTPException, status
from src.core.config import DEFAULT_VIDEO_BYTES_CAP
from src.integrations import memoir_repository, narrative_repository, participant_repository
from src.schemas.memoir import MemoirCreateRequest

# publish_memoir_tx() (migrations/a1c9e4f27b3d) raises these distinct SQLSTATEs
# so failures can be mapped to the right HTTP status without parsing English
# error text out of whatever shape postgrest happens to wrap it in.
_PUBLISH_ERROR_STATUS = {
    "P0001": (status.HTTP_404_NOT_FOUND, "Memoir not found."),
    "P0002": (status.HTTP_404_NOT_FOUND, "Memoir not found."),
    "P0003": (status.HTTP_409_CONFLICT, "This memoir has already been published."),
    "P0004": (status.HTTP_409_CONFLICT, "Add at least one memory before publishing."),
}


def _raise_for_publish_error(exc: Exception) -> None:
    """
    Extracts a Postgres SQLSTATE from a postgrest/RPC exception across the
    several shapes different client versions use to carry it (mirrors the
    defensive lookup already used in integrations/proposal_repository.py's
    _is_unique_violation, for the same reason: no single attribute is reliable
    across versions). Falls through to a generic 500 for anything unrecognized
    rather than ever turning an unexpected DB error into a misleading 404/409.
    """
    code = None
    for attr in ("code", "sqlstate", "sql_state"):
        value = getattr(exc, attr, None)
        if value:
            code = str(value)
            break

    if not code:
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            code = details.get("code")

    mapped = _PUBLISH_ERROR_STATUS.get(code)
    if mapped:
        http_status, detail = mapped
        raise HTTPException(status_code=http_status, detail=detail)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to publish memoir: {str(exc)}",
    )

class MemoirService:
    """
    Handles business logic for memoir creation, account profile resolution,
    and automatic participant role assignments.
    """

    @staticmethod
    def create_memoir(payload: MemoirCreateRequest, user_session: dict) -> dict:
        """
        Validates user session authentication, fetches creator profile details, 
        inserts a new memoir container record, and automatically registers the creator 
        as an authorized participant with the 'owner' role.

        Args:
            payload (MemoirCreateRequest): The validated memoir creation request data.
            user_session (dict): The active user session dictionary containing the user ID.

        Returns:
            dict: The newly created root memoir database record.

        Raises:
            HTTPException (401): If the user session is missing a valid user ID.
            HTTPException (500): If database insertion or participant registration fails.
        """
        user_id = user_session.get("user_id")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User session is missing user ID."
            )

        # 1. Fetch user account details using repository
        try:
            user_res = memoir_repository.fetch_user_account(user_id)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch user account details: {str(e)}"
            )

        user_record = user_res.data[0] if user_res and user_res.data else {}

        if not user_record:
            # The auth.users -> public.user_account provisioning trigger
            # (handle_new_user) isn't firing for every signup on this
            # project -- confirmed by a real 23503 FK violation here (memoir
            # insert referencing a created_by_user_id with no matching
            # user_account row). Rather than depend on that trigger being
            # reliable, self-heal it here from the JWT's own claims (the
            # same email/full_name the trigger would have used) so memoir
            # creation never hard-fails on missing infra plumbing.
            full_name = (
                (user_session.get("claims") or {}).get("user_metadata", {}).get("full_name")
                or (user_session.get("email") or "Memoir Owner").split("@")[0]
            )
            try:
                provision_res = memoir_repository.provision_user_account(
                    user_id=user_id,
                    email=user_session.get("email"),
                    full_name=full_name,
                )
                user_record = provision_res.data[0] if provision_res.data else {}
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to provision user account: {str(e)}"
                )

        display_name = user_record.get("full_name") or "Memoir Owner"
        user_email = user_record.get("email")

        # 2. Prepare root memoir payload
        memoir_data = {
            "subject_name": payload.subject_name,
            "subject_born_on": str(payload.subject_born_on) if payload.subject_born_on else None,
            "subject_died_on": str(payload.subject_died_on) if payload.subject_died_on else None,
            "subject_is_living": payload.subject_is_living,
            "description": payload.description,
            "visibility": payload.visibility,
            "comment_policy": payload.comment_policy,
            "video_bytes_cap": DEFAULT_VIDEO_BYTES_CAP,
            "created_by_user_id": user_id,
            "status": "draft"
        }

        try:
            db_response = memoir_repository.insert_memoir(memoir_data)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Database error while creating memoir: {str(e)}")

        created_memoir = db_response.data[0]
        memoir_id = created_memoir["id"]

        participant_data = {
            "memoir_id": memoir_id,
            "user_id": user_id,
            "role": "owner",
            "display_name": display_name,
            "email": user_email,
            "relationship": payload.relationship
        }

        try:
            memoir_repository.insert_memoir_participant(participant_data)
        except Exception as e:
            # COMPENSATION ROLLBACK: Delete the orphan memoir if participant insertion fails
            try:
                memoir_repository.delete_memoir_record(memoir_id)
            except Exception:
                pass  # Fallback log if cleanup fails
            
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to register memoir owner. Operation rolled back: {str(e)}"
            )

        return created_memoir

    @staticmethod
    def list_user_memoirs(user_id: str) -> list:
        """
        Returns every memoir this user actively participates in. Lets the
        frontend tell "returning user with an existing memoir" apart from
        "brand new user who needs onboarding" without relying on anything
        stashed client-side.
        """
        try:
            res = memoir_repository.fetch_memoirs_for_user(user_id)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch memoirs: {str(e)}"
            )
        rows = res.data or []
        return [row["memoir"] for row in rows if row.get("memoir")]

    @staticmethod
    def publish_memoir(memoir_id: str, user_id: str) -> dict:
        """
        Owner-only. Publishing is IRREVERSIBLE: it flips status to
        'published' (locking memories/media/transcripts via the immutability
        trigger, migrations/d8b7b48a10ea), generates the share link, and
        flags the memoir exportable to PDF -- all three in one transaction
        via publish_memoir_tx() (migrations/a1c9e4f27b3d), so a crash
        mid-publish can never leave a memoir published with no share link.

        A second publish attempt is rejected with 409, not treated as a
        no-op repeat -- re-clicking "share" on an already-published memoir
        should fetch the existing link (ShareService.create_or_get_share_link
        already does that), not re-run publish.

        Returns 404 (not 403) on a non-owner/nonexistent memoir, matching the
        pattern used by export and AI-organization access checks elsewhere.
        """
        participant_res = participant_repository.fetch_participant(memoir_id, user_id)
        participants = participant_res.data or []
        if not participants or participants[0].get("role") != "owner":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        status_res = memoir_repository.fetch_memoir_status(memoir_id)
        memoir = status_res.data[0] if status_res.data else None
        if not memoir:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        if memoir.get("status") == "published":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This memoir has already been published.",
            )

        if memoir_repository.fetch_submitted_memory_count(memoir_id) == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Add at least one memory before publishing.",
            )

        # Publishing is irreversible and the narrative is AI-written text.
        # Nobody should be able to publish an unread AI biography of their
        # dead parent -- so if any narrative sections exist, the owner must
        # have explicitly marked them reviewed first.
        if narrative_repository.narrative_sections_exist(memoir_id):
            if memoir_repository.fetch_memoir_narrative_reviewed_at(memoir_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Please review the AI-written narrative before publishing.",
                )

        # Generated here (not left to a Postgres column default) per spec:
        # the share token must come from secrets.token_urlsafe(32), the same
        # primitive used for the reader JWT secret elsewhere in this app.
        token = secrets.token_urlsafe(32)

        try:
            db_response = memoir_repository.publish_memoir_tx(memoir_id, user_id, token)
        except Exception as e:
            _raise_for_publish_error(e)
            raise  # _raise_for_publish_error always raises; satisfies static analysis

        data = db_response.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        return data