from typing import Dict, Any, Optional, List
from fastapi import HTTPException, status
from src.integrations.supabase_client import supabase_admin
from src.integrations import storage_adapter
from src.integrations import narrative_repository

class ShareRepository:

    @staticmethod
    async def get_owner_participant(memoir_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Resolves the OWNER participant row for this (memoir, user) pair --
        `role == 'owner'` is part of the query predicate itself, not a
        separate Python check afterwards. A non-owner participant row (or no
        row at all) comes back as None either way, so the caller cannot
        accidentally branch on a non-owner's id/role by forgetting a check.
        """
        try:
            res = supabase_admin.table("memoir_participant")\
                .select("id, role")\
                .eq("memoir_id", memoir_id)\
                .eq("user_id", user_id)\
                .eq("role", "owner")\
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

        One batched signing call for every asset on the page, not one per
        asset -- see storage_adapter.create_playback_urls_batch.
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

        all_keys = [
            link["media_asset"]["storage_key"]
            for memory in memories
            for link in (memory.get("memory_media") or [])
            if link.get("media_asset") and link["media_asset"].get("storage_key")
        ]
        signed_urls = storage_adapter.create_playback_urls_batch(all_keys)

        for memory in memories:
            raw_links = memory.pop("memory_media", None) or []
            media_list = []
            for link in raw_links:
                asset = link.get("media_asset")
                if not asset:
                    continue
                storage_key = asset.pop("storage_key", None)
                asset["playback_url"] = signed_urls.get(storage_key) if storage_key else None
                media_list.append(asset)
            memory["media"] = media_list
        return memories

    @staticmethod
    async def get_full_memoir_view(memoir_id: str) -> Dict[str, Any]:
        """
        Everything the reader's memoir page renders in one call: chapters,
        narrative sections (with their citation ids), and memories grouped
        by chapter with signed media. A FIXED number of queries regardless
        of how many memories/chapters exist: one for chapters, one for
        narrative sections, one for their source citations, one for
        memories, one for media links, one for media assets, one for
        transcripts, and one batched signed-URL call -- eight, whether the
        memoir has 3 memories or 300.
        """
        chapters_res = (
            supabase_admin.table("chapter")
            .select("id, title, summary, sort_order")
            .eq("memoir_id", memoir_id)
            .order("sort_order")
            .execute()
        )
        chapters = chapters_res.data or []

        memories_res = (
            supabase_admin.table("memory")
            .select(
                "id, title, body_text, occurred_start, occurred_end, occurred_precision, "
                "chapter_id, position_in_chapter, created_at, "
                "memory_media(media_asset(id, kind, mime_type, caption, duration_ms, width_px, height_px, storage_key))"
            )
            .eq("memoir_id", memoir_id)
            .eq("status", "submitted")
            .is_("deleted_at", None)
            .execute()
        )
        memories = memories_res.data or []

        audio_asset_ids = []
        all_keys = []
        for memory in memories:
            for link in memory.get("memory_media") or []:
                asset = link.get("media_asset")
                if not asset:
                    continue
                if asset.get("storage_key"):
                    all_keys.append(asset["storage_key"])
                if asset.get("kind") == "audio":
                    audio_asset_ids.append(asset["id"])

        signed_urls = storage_adapter.create_playback_urls_batch(all_keys)

        transcripts_by_asset: Dict[str, str] = {}
        if audio_asset_ids:
            t_res = (
                supabase_admin.table("transcript")
                .select("media_asset_id, display_text")
                .in_("media_asset_id", audio_asset_ids)
                .execute()
            )
            for row in t_res.data or []:
                if row.get("display_text"):
                    transcripts_by_asset[row["media_asset_id"]] = row["display_text"]

        for memory in memories:
            raw_links = memory.pop("memory_media", None) or []
            media_list = []
            for link in raw_links:
                asset = link.get("media_asset")
                if not asset:
                    continue
                storage_key = asset.pop("storage_key", None)
                asset["playback_url"] = signed_urls.get(storage_key) if storage_key else None
                if asset.get("kind") == "audio":
                    asset["transcript_text"] = transcripts_by_asset.get(asset["id"])
                media_list.append(asset)
            memory["media"] = media_list

        narrative_sections = narrative_repository.fetch_narrative_sections(memoir_id)

        return {
            "chapters": chapters,
            "memories": memories,
            "narrative_sections": narrative_sections,
        }