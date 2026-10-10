"""
@file repositories/comments_repository.py
@description Handles comment queries and secure participant/reader resolution.
"""

from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status
from src.integrations.supabase_client import supabase_admin
from src.core.reader_auth import ShareContext

class CommentsRepository:

    @staticmethod
    async def get_memory_memoir_context(memory_id: str) -> Optional[Dict[str, Any]]:
        """Resolves which memoir a memory belongs to, so access can be checked before memoir_id is known."""
        res = supabase_admin.table("memory")\
            .select("id, memoir_id")\
            .eq("id", memory_id)\
            .is_("deleted_at", None)\
            .maybe_single()\
            .execute()
        return res.data if res else None

    @staticmethod
    async def get_narrative_section_memoir_context(section_id: str) -> Optional[Dict[str, Any]]:
        """Same resolution, for a comment targeting a narrative section instead of a memory."""
        res = supabase_admin.table("narrative_section")\
            .select("id, memoir_id")\
            .eq("id", section_id)\
            .maybe_single()\
            .execute()
        return res.data if res else None

    @staticmethod
    async def get_comments(
        memory_id: Optional[str] = None, narrative_section_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetches raw comments for exactly one target, without database embedding to avoid cache sync issues."""
        try:
            query = supabase_admin.table("comment").select("*")
            if memory_id:
                query = query.eq("memory_id", memory_id)
            if narrative_section_id:
                query = query.eq("narrative_section_id", narrative_section_id)
            response = query\
                .is_("deleted_at", None)\
                .is_("hidden_at", None)\
                .order("created_at", desc=False)\
                .execute()

            data = response.data or []
            formatted_comments = []

            for item in data:
                comment_record = {**item}
                # No placeholder fallback: a comment's author_display_name is
                # always captured at insert time (owner's account name, or the
                # reader's name from their signed token). A row with none is a
                # genuine data gap, not something to paper over with a fake name.
                comment_record["author_name"] = comment_record.get("author_display_name")
                formatted_comments.append(comment_record)

            return formatted_comments

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error while fetching comments: {str(e)}"
            )

    @staticmethod
    async def insert_owner_comment(payload: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        """
        Securely resolves the user's memoir_participant_id (and display name) for the
        given memoir and inserts the comment.
        """
        try:
            memoir_id = str(payload["memoir_id"])

            participant_res = supabase_admin.table("memoir_participant")\
                .select("id, display_name")\
                .eq("memoir_id", memoir_id)\
                .eq("user_id", user_id)\
                .is_("removed_at", None)\
                .execute()

            participants = participant_res.data or []
            if not participants:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not an authorized participant of this memoir."
                )

            participant = participants[0]

            insert_data = {
                "memoir_id": memoir_id,
                "memory_id": str(payload["memory_id"]) if payload.get("memory_id") else None,
                "narrative_section_id": str(payload["narrative_section_id"]) if payload.get("narrative_section_id") else None,
                "media_asset_id": str(payload["media_asset_id"]) if payload.get("media_asset_id") else None,
                "parent_comment_id": str(payload["parent_comment_id"]) if payload.get("parent_comment_id") else None,
                "author_participant_id": participant["id"],
                "author_display_name": participant["display_name"],
                "body": payload["body"].strip()
            }

            return await CommentsRepository._insert(insert_data)

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error inserting comment: {str(e)}"
            )

    @staticmethod
    async def insert_reader_comment(payload: Dict[str, Any], share_context: ShareContext) -> Dict[str, Any]:
        """
        Inserts a comment authored by an unauthenticated reader. The author name is
        taken exclusively from the signed reader token (never the request body) and
        stored on the row so it survives the token's expiry.
        """
        try:
            insert_data = {
                "memoir_id": str(payload["memoir_id"]),
                "memory_id": str(payload["memory_id"]) if payload.get("memory_id") else None,
                "narrative_section_id": str(payload["narrative_section_id"]) if payload.get("narrative_section_id") else None,
                "media_asset_id": str(payload["media_asset_id"]) if payload.get("media_asset_id") else None,
                "parent_comment_id": str(payload["parent_comment_id"]) if payload.get("parent_comment_id") else None,
                "author_participant_id": None,
                "author_display_name": share_context.display_name,
                "body": payload["body"].strip()
            }
            return await CommentsRepository._insert(insert_data)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error inserting comment: {str(e)}"
            )

    @staticmethod
    async def _insert(insert_data: Dict[str, Any]) -> Dict[str, Any]:
        # .insert() already returns the inserted row (ReturnMethod.representation
        # is the client default) — chaining .select("*") after .insert() is not
        # supported by the installed postgrest-py version and raises AttributeError.
        response = supabase_admin.table("comment")\
            .insert(insert_data)\
            .execute()

        data = response.data or []
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to save comment record."
            )

        result = {**data[0]}
        result["author_name"] = result.get("author_display_name")
        return result

    @staticmethod
    async def fetch_comment(comment_id: str, memoir_id: str) -> Optional[Dict[str, Any]]:
        res = supabase_admin.table("comment")\
            .select("*")\
            .eq("id", comment_id)\
            .eq("memoir_id", memoir_id)\
            .execute()
        return res.data[0] if res.data else None

    @staticmethod
    async def hide_comment(comment_id: str, memoir_id: str, hidden_by_participant_id: str) -> Dict[str, Any]:
        """Owner moderation: hides a comment (soft) without deleting it outright."""
        from datetime import datetime, timezone

        res = supabase_admin.table("comment")\
            .update({
                "hidden_at": datetime.now(timezone.utc).isoformat(),
                "hidden_by_participant_id": hidden_by_participant_id,
            })\
            .eq("id", comment_id)\
            .eq("memoir_id", memoir_id)\
            .execute()
        if not res.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
        return res.data[0]

    @staticmethod
    async def delete_comment(comment_id: str, memoir_id: str) -> None:
        """Owner moderation: soft-deletes a comment outright."""
        from datetime import datetime, timezone

        res = supabase_admin.table("comment")\
            .update({"deleted_at": datetime.now(timezone.utc).isoformat()})\
            .eq("id", comment_id)\
            .eq("memoir_id", memoir_id)\
            .execute()
        if not res.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
