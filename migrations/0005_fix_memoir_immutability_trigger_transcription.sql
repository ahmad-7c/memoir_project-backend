-- 0005_fix_memoir_immutability_trigger_transcription.sql
--
-- Fix for a bug in 0001_memoir_immutability_trigger.sql: the AssemblyAI
-- transcription pipeline (src/domain/transcription_service.py) runs as a
-- background task and can still be in flight -- or not yet started -- when a
-- memoir gets published. Because the immutability trigger blocked every
-- write to media_asset/transcript once a memoir was published, a recording
-- uploaded before publication could get permanently stuck: the pipeline's
-- own status/content writes were rejected with the same "published and
-- immutable" error meant for user edits.
--
-- This replaces the trigger function (create or replace, same name -- the
-- existing triggers on memory/media_asset/memory_media/transcript keep
-- pointing at it, no need to recreate them) so it narrowly exempts:
--   - media_asset: an UPDATE that changes only transcription_status
--   - transcript: an INSERT/UPDATE that doesn't touch the owner-correction
--     columns (edited_text/edited_by_participant_id/edited_at)
-- Everything else -- new uploads, deletes, owner edits, changes to any other
-- column -- is still blocked exactly as before.
--
-- Run this manually against Supabase (SQL Editor or psql), same as 0001.

create or replace function public.prevent_writes_to_published_memoir()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_memoir_id uuid;
  v_status public.memoir_status;
  v_transcription_pipeline_write boolean := false;
begin
  v_memoir_id := coalesce(new.memoir_id, old.memoir_id);

  select status into v_status
  from public.memoir
  where id = v_memoir_id;

  if v_status = 'published' then
    if tg_table_name = 'media_asset' and tg_op = 'UPDATE' then
      v_transcription_pipeline_write :=
        new.transcription_status is distinct from old.transcription_status
        and new.id = old.id
        and new.memoir_id = old.memoir_id
        and new.kind = old.kind
        and new.storage_key = old.storage_key
        and new.mime_type = old.mime_type
        and new.byte_size = old.byte_size
        and new.checksum_sha256 is not distinct from old.checksum_sha256
        and new.original_filename is not distinct from old.original_filename
        and new.duration_ms is not distinct from old.duration_ms
        and new.width_px is not distinct from old.width_px
        and new.height_px is not distinct from old.height_px
        and new.caption is not distinct from old.caption
        and new.storage_tier = old.storage_tier
        and new.tier_changed_at is not distinct from old.tier_changed_at
        and new.uploaded_by_participant_id = old.uploaded_by_participant_id
        and new.deleted_at is not distinct from old.deleted_at;
    elsif tg_table_name = 'transcript' and tg_op = 'INSERT' then
      -- The pipeline's own inserts never set an owner correction.
      v_transcription_pipeline_write :=
        new.edited_text is null
        and new.edited_by_participant_id is null
        and new.edited_at is null;
    elsif tg_table_name = 'transcript' and tg_op = 'UPDATE' then
      v_transcription_pipeline_write :=
        new.edited_text is not distinct from old.edited_text
        and new.edited_by_participant_id is not distinct from old.edited_by_participant_id
        and new.edited_at is not distinct from old.edited_at;
    end if;

    if not v_transcription_pipeline_write then
      raise exception 'This memoir has been published and can no longer be changed.'
        using errcode = 'P0001';
    end if;
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;

  return new;
end;
$$;
