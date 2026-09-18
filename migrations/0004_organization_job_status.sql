-- 0004_organization_job_status.sql
--
-- SUPERSEDED: this change now runs through Alembic instead of manually --
-- see alembic/versions/cd3760273110_organization_job_status.py. Applied to
-- the shared sandbox project via `alembic upgrade head` on 2026-09-18. Kept
-- here only as a historical record of the raw SQL; do not re-run this file.
--
-- The reference AI-organization branch (feature/backend-AI-organization) ran
-- the Gemini clustering job via BackgroundTasks but never persisted the job's
-- outcome anywhere -- a caller had no way to know it finished, failed, or
-- died with the API process (R6/R7 of the organize-feature review).
--
-- Mirrors the existing transcript pattern (see migrations/0002 and
-- src/domain/transcription_service.py): reuse the row the job is *about*
-- (here, the memoir) rather than a separate jobs table, and compute a
-- "stalled" presentation status at read time from organization_started_at
-- instead of storing it -- a job stuck in 'running' past 5 minutes is dead
-- (BackgroundTasks has no crash recovery / heartbeat).
--
-- Reuses the existing public.job_status enum (queued/running/ready/failed)
-- already used by memoir_export -- no new enum needed.
--
-- Run this manually against Supabase (SQL Editor or psql), same as 0002/0003.

alter table public.memoir
  add column if not exists organization_status public.job_status,
  add column if not exists organization_started_at timestamptz,
  add column if not exists organization_completed_at timestamptz,
  add column if not exists organization_error_message text;
