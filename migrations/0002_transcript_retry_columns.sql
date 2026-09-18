-- 0002_transcript_retry_columns.sql
--
-- Feature Request 03: transcription failures must be visible (not swallowed),
-- carry a human-readable message, and support a bounded manual retry.
--
-- Adds retry/failure bookkeeping to the existing transcript table. Reuses
-- existing columns where they already cover the concept instead of
-- duplicating them:
--   - status lives on media_asset.transcription_status (already exists:
--     pending/processing/ready/failed/skipped) — no separate status column
--     added here, to avoid two sources of truth that can drift apart.
--   - "the owner's correctable version" is the existing edited_text column;
--     display_text is kept as the resolved value the frontend renders
--     (edited_text when set, otherwise the latest raw_text).
--   - "provider" is the existing engine column (currently always
--     'assemblyai').
--
-- Run this manually against Supabase (SQL Editor or psql). Not applied via
-- Alembic in this pass.

alter table public.transcript
  add column if not exists error_message text,
  add column if not exists attempt_count integer not null default 0,
  add column if not exists provider_job_id text,
  add column if not exists updated_at timestamp with time zone;
