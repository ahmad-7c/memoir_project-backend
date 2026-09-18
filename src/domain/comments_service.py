"""
@file services/comments_service.py
@description Business logic layer for comment processing.
"""

from typing import List, Dict, Any, Optional
from src.integrations.comments_repository import CommentsRepository
from src.domain.access_control import resolve_memoir_access
from fastapi import HTTPException, status

class CommentsService:

    @staticmethod
    async def get_memory_comments(memory_id: str, authorization: Optional[str]) -> List[Dict[str, Any]]:
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
        resolve_memoir_access(memoir_id=str(context["memoir_id"]), authorization=authorization)

        return await CommentsRepository.get_comments_by_memory_id(memory_id)

    @staticmethod
    async def create_new_comment(payload: Dict[str, Any], authorization: Optional[str]) -> Dict[str, Any]:
        if not payload.get("body") or not payload["body"].strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Comment text cannot be empty."
            )

        memory_id = payload.get("memory_id")
        context = await CommentsRepository.get_memory_memoir_context(memory_id) if memory_id else None
        if not context:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

        memoir_id = str(context["memoir_id"])
        if str(payload.get("memoir_id")) != memoir_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

        access = resolve_memoir_access(memoir_id=memoir_id, authorization=authorization)

        if access.kind == "reader":
            if not access.share_context.can_comment:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Commenting is not enabled for this memoir."
                )
            return await CommentsRepository.insert_reader_comment(payload, access.share_context)

        return await CommentsRepository.insert_owner_comment(payload, access.user_id)
