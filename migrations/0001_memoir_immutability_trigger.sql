-- 0001_memoir_immutability_trigger.sql
--
-- Feature Request 04: a published memoir is immutable. Comments are the only
-- thing that may still be added after publication.
--
-- This is layer 2 of two (see src/domain/authorization.py::assert_memoir_editable
-- for layer 1, the application-level guard). Layer 1 depends on every write path
-- remembering to call it; this trigger does not — it fires regardless of which
-- code path, client, or hand-run query performed the write.
--
-- Run this manually against Supabase (SQL Editor or psql). Not applied via
-- Alembic in this pass.

create or replace function public.prevent_writes_to_published_memoir()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_memoir_id uuid;
  v_status public.memoir_status;
begin
  v_memoir_id := coalesce(new.memoir_id, old.memoir_id);

  select status into v_status
  from public.memoir
  where id = v_memoir_id;

  if v_status = 'published' then
    raise exception 'This memoir has been published and can no longer be changed.'
      using errcode = 'P0001';
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;

  return new;
end;
$$;

drop trigger if exists trg_memory_immutable on public.memory;
create trigger trg_memory_immutable
  before insert or update or delete on public.memory
  for each row execute function public.prevent_writes_to_published_memoir();

drop trigger if exists trg_media_asset_immutable on public.media_asset;
create trigger trg_media_asset_immutable
  before insert or update or delete on public.media_asset
  for each row execute function public.prevent_writes_to_published_memoir();

drop trigger if exists trg_memory_media_immutable on public.memory_media;
create trigger trg_memory_media_immutable
  before insert or update or delete on public.memory_media
  for each row execute function public.prevent_writes_to_published_memoir();

drop trigger if exists trg_transcript_immutable on public.transcript;
create trigger trg_transcript_immutable
  before insert or update or delete on public.transcript
  for each row execute function public.prevent_writes_to_published_memoir();
