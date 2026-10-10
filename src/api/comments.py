"""
@file routers/comments_router.py
@description FastAPI router endpoints for comment operations.
"""

from fastapi import APIRouter, Depends, Query, Request, status, Header
from typing import List, Optional
from src.core.auth import get_current_user
from src.core.config import ACCESS_TOKEN_COOKIE_NAME
from src.schemas.comments import CommentCreate, CommentResponse
from src.domain.comments_service import CommentsService

router = APIRouter(prefix="/api/comments", tags=["Comments"])

@router.get("/", response_model=List[CommentResponse])
async def list_comments(
    request: Request,
    memory_id: str = Query(..., description="The UUID of the memory item"),
    authorization: Optional[str] = Header(None)
):
    """
    Fetch all comments linked to a specific memory asset. Accepts EITHER a valid
    reader (share-link) token for the memoir that owns this memory, OR an owner JWT
    with verified ownership -- the owner's JWT normally arrives via the httpOnly
    `access_token` cookie (browser JS never sees it to put in a header), so that
    cookie is passed as a fallback alongside the header.
    """
    cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    return await CommentsService.get_memory_comments(memory_id, authorization, cookie_token)

@router.post("/", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def post_comment(
    payload: CommentCreate,
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    Post a new comment to a memory item. A reader needs a valid share token AND the
    owner must have commenting enabled; an owner needs an active participant record
    (via the Authorization header or, for a browser session, the httpOnly cookie).
    The author's display name always comes from the resolved caller identity, never
    from the request body.
    """
    cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    return await CommentsService.create_new_comment(payload.dict(), authorization, cookie_token)


@router.patch("/{comment_id}/hide", status_code=status.HTTP_200_OK)
async def hide_comment(
    comment_id: str,
    memoir_id: str = Query(..., description="The memoir this comment belongs to."),
    current_user: dict = Depends(get_current_user),
):
    """Owner-only: hides a comment without deleting it outright."""
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    result = await CommentsService.moderate_comment(comment_id, memoir_id, user_id, "hide")
    return {"success": True, "message": "Comment hidden.", "data": result}


@router.delete("/{comment_id}", status_code=status.HTTP_200_OK)
async def delete_comment(
    comment_id: str,
    memoir_id: str = Query(..., description="The memoir this comment belongs to."),
    current_user: dict = Depends(get_current_user),
):
    """Owner-only: removes a comment."""
    user_id = current_user.get("user_id") or current_user.get("id") or current_user.get("sub")
    await CommentsService.moderate_comment(comment_id, memoir_id, user_id, "delete")
    return {"success": True, "message": "Comment removed."}
