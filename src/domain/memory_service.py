"""
@file memory_service.py
@description Core business logic service managing memory creation with strict 
date timeline normalization, media asset linking, feed retrieval, and lifecycle security,
fully decoupled from direct database infrastructure calls.
"""

from fastapi import HTTPException, status
from src.schemas.memory import MemoryCreateRequest
from src.integrations import memory_repository, participant_repository
from src.domain.authorization import verify_active_participant, assert_memoir_editable
from src.integrations import storage_adapter
from src.domain.transcription_service import enqueue_transcription, compute_effective_transcription_status
from src.integrations.supabase_client import supabase  # <-- Required for querying transcript table directly if needed

class MemoryService:
    """
    Handles business logic for memory stories, including participant security enforcement,
    timeline normalization, media-to-memory junction mapping, and feed processing.
    """

    @classmethod
    def create_memory(cls, payload: MemoryCreateRequest, user_id: str, background_tasks=None) -> dict:
        """
        Validates participant permissions, normalizes timeline and date parameters,
        persists the new memory entry, and maps any attached media asset IDs.
        """
        participant = verify_active_participant(
            str(payload.memoir_id),
            user_id,
            required_roles=["owner", "admin", "contributor"]
        )
        assert_memoir_editable(str(payload.memoir_id))
        participant_id = participant["id"]

        # Timeline Date: Pass through exactly what the user sent without inventing defaults
        memory_data = {
            "memoir_id": str(payload.memoir_id),
            "author_participant_id": participant_id, 
            "title": payload.title,
            "body_text": payload.body_text,
            "status": payload.status,
            "occurred_start": payload.occurred_start.isoformat() if payload.occurred_start else None,
            "occurred_end": payload.occurred_end.isoformat() if payload.occurred_end else None,
            "occurred_precision": payload.occurred_precision,
            "date_source": payload.date_source,
        }
        
        try:
            mem_res = memory_repository.insert_memory(memory_data)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error while creating memory: {str(e)}"
            )

        if not mem_res or not mem_res.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create memory record."
            )

        new_memory = mem_res.data[0]
        memory_id = new_memory["id"]

        # Associate attached media assets via the junction table with ownership verification
        if payload.media_asset_ids:
            print(f"DEBUG: Found media_asset_ids in payload: {payload.media_asset_ids}")
            
            owned_assets = memory_repository.verify_media_assets_belong_to_memoir(
                payload.memoir_id, payload.media_asset_ids
            )
            
            if len(owned_assets) != len(payload.media_asset_ids):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="One or more media assets do not belong to this memoir container."
                )

            link_records = [
            {
                "memory_id": str(memory_id),        
                "media_asset_id": str(media_id),    
                "memoir_id": str(payload.memoir_id) 
            }
            for media_id in payload.media_asset_ids
            ]
            
            try:
                memory_repository.insert_memory_media(link_records)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to link media assets to memory: {str(e)}"
                )

            # Queue transcription for any newly-attached audio assets. This runs
            # AFTER insert_memory_media has committed above, and only actually
            # executes once this request has returned a response (BackgroundTasks
            # semantics) — the browser never waits on AssemblyAI.
            if background_tasks is not None:
                for media_id in payload.media_asset_ids:
                    asset_record = memory_repository.fetch_media_asset_record(str(media_id))
                    if asset_record and asset_record.get("kind") == "audio" and asset_record.get("storage_key"):
                        enqueue_transcription(
                            media_asset_id=str(media_id),
                            memoir_id=str(payload.memoir_id),
                            storage_key=asset_record["storage_key"],
                            background_tasks=background_tasks,
                        )
        return new_memory
    
    @classmethod
    def get_memoir_feed(cls, memoir_id: str, user_id: str, limit: int = 20, offset: int = 0) -> list:
        """
        Retrieves a paginated memoir memory feed for an active participant, 
        hydrating all attached media assets with secure signed playback URLs and AI transcripts.
        """
        # 1. Enforce active participant check
        verify_active_participant(str(memoir_id), str(user_id))

        # 2. Fetch paginated memory records using your repository function
        try:
            res = memory_repository.fetch_memoir_feed_records(str(memoir_id), limit, offset)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch memoir feed: {str(e)}"
            )

        memories = res.data if res and res.data else []

        # 3a. First pass: hydrate playback URLs, and collect every audio asset ID
        # up front instead of querying its transcript one at a time in the loop
        # below (FR03: "a single database request rather than separate requests
        # for each item" — query count must not grow with the number of memories).
        audio_asset_ids = []
        for mem in memories:
            media_list = []
            raw_links = mem.pop("memory_media", [])
            for link in raw_links:
                asset = link.get("media_asset")
                if not asset:
                    continue

                storage_key = asset.get("storage_key")
                try:
                    asset["playback_url"] = storage_adapter.create_playback_url(storage_key) if storage_key else None
                except Exception:
                    asset["playback_url"] = None

                if asset.get("kind") == "audio":
                    audio_asset_ids.append(asset["id"])

                media_list.append(asset)
            mem["media_assets"] = media_list

        # 3b. One batched transcript fetch for every audio asset across the whole page.
        transcripts_by_asset_id = {}
        if audio_asset_ids:
            try:
                transcripts_res = supabase.table("transcript").select("*").in_("media_asset_id", audio_asset_ids).execute()
                for row in (transcripts_res.data or []):
                    transcripts_by_asset_id[row["media_asset_id"]] = row
            except Exception:
                transcripts_by_asset_id = {}

        # 3c. Second pass: attach each asset's transcript (or None) from the map,
        # and compute the effective status (overrides a stalled 'processing' job —
        # BackgroundTasks has no worker heartbeat, so this is how a dead job
        # surfaces instead of spinning forever).
        for mem in memories:
            for asset in mem["media_assets"]:
                if asset.get("kind") == "audio":
                    asset["transcript"] = transcripts_by_asset_id.get(asset["id"])
                    asset["transcription_status"] = compute_effective_transcription_status(asset)
                else:
                    asset["transcript"] = None

        return memories
        
    @classmethod
    def delete_memory(cls, memoir_id: str, memory_id: str, user_id: str) -> dict:
        """
        Safely soft-deletes a memory record after confirming the user is either 
        the memory's author or a memoir owner/admin.
        """
        participant_res = participant_repository.fetch_participant(memoir_id, user_id)
        if not participant_res.data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a participant of this memoir."
            )
        
        participant = participant_res.data[0]
        user_role = participant.get("role")
        participant_id = participant.get("id")

        assert_memoir_editable(memoir_id)

        try:
            mem_res = memory_repository.fetch_memory_by_id(memory_id, memoir_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        if not mem_res.data:
            raise HTTPException(status_code=404, detail="Memory not found in this memoir.")

        memory = mem_res.data[0]

        is_owner_or_admin = user_role in ["owner", "admin"]
        is_author = (
            memory.get("author_user_id") == user_id or 
            memory.get("author_participant_id") == participant_id
        )

        if not (is_owner_or_admin or is_author):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete this memory."
            )

        if memory.get("status") == "submitted":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete a submitted/finalized memory."
            )

        try:
            memory_repository.soft_delete_memory_record(memory_id, memoir_id)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete memory: {str(e)}"
            )
            
        return {"success": True, "message": "Memory successfully deleted."}