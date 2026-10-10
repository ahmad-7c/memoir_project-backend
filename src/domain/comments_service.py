"""
@file services/comments_service.py
@description Business logic layer for comment processing on the OWNER-facing
dual-access routes (/api/comments/). Reader-facing comment access goes
through the standalone domain/reader_access.py dependency and talks to
CommentsRepository directly from api/share.py -- see that module's docstring
for why these are deliberately two separate code paths rather than one
shared here.
"""

from typing import List, Dict, Any, Optional
from src.integrations.comments_repository import CommentsRepository
from src.domain.access_control import resolve_memoir_access
from fastapi import HTTPException, status

class CommentsService:

    @staticmethod
    async def _resolve_target_memoir_id(payload: Dict[str, Any]) -> str:
        """Exactly one of memory_id / narrative_section_id, resolved to its memoir."""
        memory_id = payload.get("memory_id")
        section_id = payload.get("narrative_section_id")

        if bool(memory_id) == bool(section_id):
            # Both set, or neither -- a comment needs exactly one target.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A comment must target exactly one memory or narrative section.",
            )

        if memory_id:
            context = await CommentsRepository.get_memory_memoir_context(memory_id)
        else:
            context = await CommentsRepository.get_narrative_section_memoir_context(section_id)

        if not context:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
        return str(context["memoir_id"])

    @staticmethod
    async def get_memory_comments(
        memory_id: str, authorization: Optional[str], cookie_token: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if not memory_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Memory ID is required."
            )

        context = await CommentsRepository.get_memory_memoir_context(memory_id)
        if not context:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

        # Reading comments follows the exact same access rule as reading the memoir
        # itself: a valid share token for this memoir, OR an owner JWT with verified
        # ownership. Readers have no accounts, so "require login" is not an option here.
        resolve_memoir_access(
            memoir_id=str(context["memoir_id"]), authorization=authorization, cookie_token=cookie_token
        )

        return await CommentsRepository.get_comments(memory_id=memory_id)

    @staticmethod
    async def create_new_comment(
        payload: Dict[str, Any], authorization: Optional[str], cookie_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if not payload.get("body") or not payload["body"].strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Comment text cannot be empty."
            )

        memoir_id = await CommentsService._resolve_target_memoir_id(payload)
        if str(payload.get("memoir_id")) != memoir_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

        access = resolve_memoir_access(
            memoir_id=memoir_id, authorization=authorization, cookie_token=cookie_token
        )

        if access.kind == "reader":
            if not access.share_context.can_comment:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Commenting is not enabled for this memoir."
                )
            return await CommentsRepository.insert_reader_comment(payload, access.share_context)

        return await CommentsRepository.insert_owner_comment(payload, access.user_id)

    @staticmethod
    async def moderate_comment(
        comment_id: str, memoir_id: str, user_id: str, action: str
    ) -> Optional[Dict[str, Any]]:
        """
        Owner-only hide/delete. 404s on a non-owner or a comment that
        doesn't belong to this memoir -- same "not found, never forbidden"
        rule as everywhere else in this codebase.
        """
        from src.domain.authorization import verify_active_participant

        participant = verify_active_participant(memoir_id, user_id, required_roles=["owner", "admin"])

        comment = await CommentsRepository.fetch_comment(comment_id, memoir_id)
        if not comment:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")

        if action == "hide":
            return await CommentsRepository.hide_comment(comment_id, memoir_id, participant["id"])
        if action == "delete":
            await CommentsRepository.delete_comment(comment_id, memoir_id)
            return None
        raise ValueError(f"Unknown moderation action: {action!r}")
