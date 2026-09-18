"""
@file routers/comments_router.py
@description FastAPI router endpoints for comment operations.
"""

from fastapi import APIRouter, Query, status, Header
from typing import List, Optional
from src.schemas.comments import CommentCreate, CommentResponse
from src.domain.comments_service import CommentsService

router = APIRouter(prefix="/api/comments", tags=["Comments"])

@router.get("/", response_model=List[CommentResponse])
async def list_comments(
    memory_id: str = Query(..., description="The UUID of the memory item"),
    authorization: Optional[str] = Header(None)
):
    """
    Fetch all comments linked to a specific memory asset. Accepts EITHER a valid
    reader (share-link) token for the memoir that owns this memory, OR an owner JWT
    with verified ownership.
    """
    return await CommentsService.get_memory_comments(memory_id, authorization)

@router.post("/", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def post_comment(
    payload: CommentCreate,
    authorization: Optional[str] = Header(None)
):
    """
    Post a new comment to a memory item. A reader needs a valid share token AND the
    owner must have commenting enabled; an owner needs an active participant record.
    The author's display name always comes from the resolved caller identity, never
    from the request body.
    """
    return await CommentsService.create_new_comment(payload.dict(), authorization)
