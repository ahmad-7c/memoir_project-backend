-- 0003_memory_media_foreign_keys.sql
--
-- memory_media.memory_id and .media_asset_id had no FK constraints at all —
-- just plain uuid columns forming the composite PK. PostgREST can't resolve
-- the memory_media(media_asset(*)) embedded-select the memory feed depends on
-- without a real FK, so GET /api/memories/feed/{memoir_id} 500s whenever a
-- memory actually has attached media.
--
-- Run this manually against Supabase (SQL Editor or psql). Not applied via
-- Alembic against the shared project in this pass.

alter table public.memory_media
  add constraint memory_media_memory_id_fkey
  foreign key (memory_id) references public.memory(id) on delete cascade;

alter table public.memory_media
  add constraint memory_media_media_asset_id_fkey
  foreign key (media_asset_id) references public.media_asset(id) on delete cascade;
