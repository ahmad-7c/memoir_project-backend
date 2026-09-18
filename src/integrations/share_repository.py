from typing import Dict, Any, Optional, List
from fastapi import HTTPException, status
from src.integrations.supabase_client import supabase_admin
from src.integrations import storage_adapter

class ShareRepository:

    @staticmethod
    async def get_participant(memoir_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Resolves the participant ID and role for the user."""
        try:
            res = supabase_admin.table("memoir_participant")\
                .select("id, role")\
                .eq("memoir_id", memoir_id)\
                .eq("user_id", user_id)\
                .is_("removed_at", None)\
                .execute()
            return res.data[0] if res.data else None
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    @staticmethod
    async def get_memoir_by_id(memoir_id: str) -> Optional[Dict[str, Any]]:
        res = supabase_admin.table("memoir").select("*").eq("id", memoir_id).execute()
        return res.data[0] if res.data else None

    @staticmethod
    async def get_active_link(memoir_id: str, scope: str) -> Optional[Dict[str, Any]]:
        """Matches your SQL constraint: one live link per scope per memoir."""
        res = supabase_admin.table("memoir_link")\
            .select("*")\
            .eq("memoir_id", memoir_id)\
            .eq("scope", scope)\
            .is_("revoked_at", None)\
            .execute()
        return res.data[0] if res.data else None

    @staticmethod
    async def get_link_by_token(token: str) -> Optional[Dict[str, Any]]:
        res = supabase_admin.table("memoir_link").select("*").eq("token", token).execute()
        return res.data[0] if res.data else None

    @staticmethod
    async def get_link_by_id(link_id: str) -> Optional[Dict[str, Any]]:
        res = supabase_admin.table("memoir_link").select("*").eq("id", link_id).execute()
        return res.data[0] if res.data else None

    @staticmethod
    async def insert_link(insert_data: Dict[str, Any]) -> Dict[str, Any]:
        # Token and defaults are generated automatically by your Postgres schema.
        # .insert() already returns the inserted row by default — chaining
        # .select("*") after .insert() isn't supported by the installed
        # postgrest-py version and raises AttributeError.
        res = supabase_admin.table("memoir_link").insert(insert_data).execute()
        if not res.data:
            raise HTTPException(status_code=400, detail="Failed to create share link")
        return res.data[0]

    @staticmethod
    async def update_link(link_id: str, update_data: Dict[str, Any]) -> Dict[str, Any]:
        # .update() already returns the updated row by default — chaining
        # .select("*") after the filter isn't supported by the installed
        # postgrest-py version and raises AttributeError.
        res = supabase_admin.table("memoir_link").update(update_data).eq("id", link_id).execute()
        return res.data[0]

    @staticmethod
    async def increment_open_count(link_id: str, current_count: int) -> None:
        supabase_admin.table("memoir_link").update({"open_count": current_count + 1}).eq("id", link_id).execute()

    @staticmethod
    async def get_shared_memoir_view(memoir_id: str) -> List[Dict[str, Any]]:
        """
        Reader-facing memory feed. Selects an explicit allowlist of columns rather
        than "*" — storage_key, storage_bucket, uploader_user_id, checksums and
        internal participant IDs must never reach an anonymous reader. Media gets a
        short-lived signed playback URL instead of its raw storage path.
        """
        res = supabase_admin.table("memory")\
            .select(
                "id, title, body_text, occurred_start, occurred_end, occurred_precision, created_at, "
                "memory_media(media_asset(id, kind, mime_type, caption, duration_ms, width_px, height_px, storage_key))"
            )\
            .eq("memoir_id", memoir_id)\
            .eq("status", "submitted")\
            .is_("deleted_at", None)\
            .order("created_at", desc=True)\
            .execute()

        memories = res.data or []
        for memory in memories:
            raw_links = memory.pop("memory_media", None) or []
            media_list = []
            for link in raw_links:
                asset = link.get("media_asset")
                if not asset:
                    continue
                storage_key = asset.pop("storage_key", None)
                asset["playback_url"] = storage_adapter.create_playback_url(storage_key) if storage_key else None
                media_list.append(asset)
            memory["media"] = media_list
        return memories