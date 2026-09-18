-- 0000_bootstrap_sandbox_schema.sql
--
-- SANDBOX-ONLY bootstrap. Run this against a fresh/empty personal Supabase
-- project, NOT against the shared team project (which already has data and a
-- schema of its own — this file would destroy it via the DROP statements
-- below).
--
-- This recreates the shared project's schema as reverse-engineered via its
-- PostgREST OpenAPI endpoint (columns, types, nullability, defaults, PK/FK
-- targets), PLUS the columns the current backend code already depends on that
-- were written in prior passes but never confirmed-applied to the shared
-- project:
--   - memoir_link.visibility / memoir_link.password_hash   (reader-password fix)
--   - comment.author_participant_id made nullable, + comment.author_display_name  (reader-comment fix)
--   - transcript.error_message / attempt_count / provider_job_id / updated_at   (transcript retry fix, = migrations/0002)
--
-- FK ON DELETE behavior, unique constraints beyond primary keys, and check
-- constraints were NEVER confirmed against the shared project (the
-- introspection query for those was never run). Every choice below is a
-- reasonable guess for sandbox purposes, marked inline — do not treat this file
-- as an authoritative description of the shared project's real constraints.

-- ============================================================================
-- 0. Reset (sandbox only)
-- ============================================================================
drop table if exists public.comment cascade;
drop table if exists public.memoir_export cascade;
drop table if exists public.memoir_link cascade;
drop table if exists public.memoir_participant cascade;
drop table if exists public.transcript cascade;
drop table if exists public.memory_media cascade;
drop table if exists public.chapter cascade;
drop table if exists public.media_asset cascade;
drop table if exists public.memory cascade;
drop table if exists public.memoir cascade;
drop table if exists public.user_account cascade;
drop table if exists public.alembic_version cascade;

-- ============================================================================
-- 1. Extensions
-- ============================================================================
create extension if not exists pgcrypto with schema extensions;
create extension if not exists citext with schema extensions;

-- ============================================================================
-- 2. Enum types
-- ============================================================================
do $$ begin
  create type public.authored_by as enum ('contributor', 'owner', 'ai');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.media_kind as enum ('audio', 'photo', 'video');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.storage_tier as enum ('hot', 'cold');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.transcode_status as enum ('pending', 'processing', 'ready', 'failed', 'skipped');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.memoir_status as enum ('draft', 'published');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.memoir_visibility as enum ('invited_only', 'link_with_password', 'link_public');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.comment_policy as enum ('nobody', 'invited_only', 'anyone_who_can_view');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.export_kind as enum ('pdf', 'raw_archive');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.job_status as enum ('queued', 'running', 'ready', 'failed');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.link_scope as enum ('contribute', 'view');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.participant_role as enum ('owner', 'co_owner', 'contributor', 'reader');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.relationship_group as enum (
    'spouse_partner', 'child', 'grandchild', 'sibling', 'parent',
    'extended_family', 'friend', 'colleague', 'neighbour', 'self', 'other'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.notify_frequency as enum ('instant', 'weekly_digest', 'off');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.memory_status as enum ('draft', 'submitted');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.date_precision as enum ('day', 'month', 'year', 'decade');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.media_link_type as enum ('primary', 'reference');
exception when duplicate_object then null; end $$;

-- ============================================================================
-- 3. Tables
-- ============================================================================

create table public.user_account (
  id                 uuid primary key default gen_random_uuid(),
  email              extensions.citext not null,
  full_name          text not null,
  auth_provider_uid  text,
  last_login_at      timestamptz,
  created_at         timestamptz not null default now(),
  deleted_at         timestamptz
);

create table public.memoir (
  id                  uuid primary key default gen_random_uuid(),
  subject_name        text not null,
  subject_born_on     date,
  subject_died_on     date,
  subject_is_living   boolean not null default false,
  description         text,
  cover_media_id      uuid,  -- FK added below, after media_asset exists (circular reference)
  status              public.memoir_status not null default 'draft',
  published_at        timestamptz,
  visibility          public.memoir_visibility not null default 'invited_only',
  view_password_hash  text,
  comment_policy      public.comment_policy not null default 'invited_only',
  video_bytes_cap     bigint not null,
  created_by_user_id  uuid not null references public.user_account(id),  -- ON DELETE: guess = RESTRICT (default)
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create table public.chapter (
  id               uuid primary key default gen_random_uuid(),
  memoir_id        uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  title            text not null,
  summary          text,
  sort_order       integer not null default 0,
  created_by       public.authored_by not null default 'ai',
  edited_by_owner  boolean not null default false,
  created_at       timestamptz not null default now(),
  narrative_prose  text
);

create table public.media_asset (
  id                           uuid primary key default gen_random_uuid(),
  memoir_id                    uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  kind                         public.media_kind not null,
  storage_key                  text not null,
  mime_type                    text not null,
  byte_size                    bigint not null,
  checksum_sha256              text,
  original_filename            text,
  duration_ms                  integer,
  width_px                     integer,
  height_px                    integer,
  caption                      text,
  storage_tier                 public.storage_tier not null default 'hot',
  tier_changed_at              timestamptz,
  transcription_status         public.transcode_status not null default 'pending',
  -- No FK on the live shared project either — faithfully replicated, not an
  -- oversight introduced here. Worth fixing in a real migration later.
  uploaded_by_participant_id   uuid not null,
  created_at                   timestamptz not null default now(),
  deleted_at                   timestamptz,
  caption_tsv                  tsvector
);

-- Circular FK: memoir.cover_media_id -> media_asset.id, added now that media_asset exists.
alter table public.memoir
  add constraint memoir_cover_media_id_fkey
  foreign key (cover_media_id) references public.media_asset(id) on delete set null;  -- ON DELETE: guess

create table public.memory (
  id                          uuid primary key default gen_random_uuid(),
  memoir_id                   uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  -- No FK on the live shared project — faithfully replicated.
  author_participant_id       uuid not null,
  prompt_id                   uuid,  -- no "prompt" table in scope; live has no FK here either
  title                       text,
  body_text                   text,
  status                      public.memory_status not null default 'draft',
  submitted_at                timestamptz,
  chapter_id                  uuid,  -- live has no FK here either, despite chapter existing
  position_in_chapter         integer,
  occurred_start               date,
  occurred_end                date,
  occurred_precision          public.date_precision,
  date_source                 public.authored_by,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now(),
  deleted_at                  timestamptz,
  deleted_by_participant_id   uuid,  -- FK added below, after memoir_participant exists
  search_tsv                  tsvector
);

create table public.memory_media (
  -- Live shared project has no FK on either half of this composite PK either —
  -- faithfully replicated, not an oversight introduced here.
  memory_id            uuid not null,
  media_asset_id       uuid not null,
  memoir_id            uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  link_type            public.media_link_type not null default 'primary',
  position             integer not null default 0,
  created_by           public.authored_by not null default 'contributor',
  confidence           numeric,
  confirmed_by_owner   boolean not null default false,
  created_at           timestamptz not null default now(),
  primary key (memory_id, media_asset_id)
);

create table public.transcript (
  media_asset_id   uuid primary key references public.media_asset(id) on delete cascade,  -- ON DELETE: guess
  memoir_id        uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  raw_text         text not null,
  edited_text      text,
  language         text not null default 'en',
  engine           text not null,
  confidence       numeric,
  edited_by_participant_id  uuid,  -- live has no FK here either
  edited_at        timestamptz,
  created_at       timestamptz not null default now(),
  display_text     text,
  search_tsv       tsvector,
  -- Added by src/domain/transcription_service.py (Fix 8 / migrations/0002_transcript_retry_columns.sql):
  error_message    text,
  attempt_count    integer not null default 0,
  provider_job_id  text,
  updated_at       timestamptz
);

create table public.memoir_participant (
  id                    uuid primary key default gen_random_uuid(),
  memoir_id             uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  role                  public.participant_role not null,
  user_id               uuid references public.user_account(id) on delete set null,  -- ON DELETE: guess
  display_name          text not null,
  relationship          public.relationship_group not null default 'other',
  relationship_label    text,
  email                 extensions.citext,
  invited_at            timestamptz,
  first_opened_at       timestamptz,
  reminded_at           timestamptz,
  notify_frequency      public.notify_frequency not null default 'instant',
  unsubscribe_token     text not null default encode(extensions.gen_random_bytes(24), 'hex'),
  created_at            timestamptz not null default now(),
  removed_at            timestamptz
);

-- Now that memoir_participant exists, wire the two FKs that referenced it.
alter table public.memory
  add constraint memory_deleted_by_participant_id_fkey
  foreign key (deleted_by_participant_id) references public.memoir_participant(id) on delete set null;  -- ON DELETE: guess

create table public.comment (
  id                          uuid primary key default gen_random_uuid(),
  memoir_id                   uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  memory_id                   uuid,  -- live has no FK here either
  media_asset_id              uuid,  -- live has no FK here either
  parent_comment_id           uuid references public.comment(id) on delete cascade,  -- ON DELETE: guess
  -- Nullable (not the live default of NOT NULL) so reader-authored comments
  -- (no participant record) can be stored. See src/integrations/comments_repository.py.
  author_participant_id       uuid,
  -- Added by the reader-comment fix: resolved display name captured at insert
  -- time from either the participant record or the signed reader token,
  -- persisted so it survives the reader token's expiry.
  author_display_name         text,
  body                         text not null,
  created_at                  timestamptz not null default now(),
  hidden_at                   timestamptz,
  hidden_by_participant_id    uuid references public.memoir_participant(id) on delete set null,  -- ON DELETE: guess
  deleted_at                  timestamptz
);

create table public.memoir_link (
  id                          uuid primary key default gen_random_uuid(),
  memoir_id                   uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  scope                       public.link_scope not null,
  token                       text not null default encode(extensions.gen_random_bytes(24), 'hex'),
  created_by_participant_id   uuid references public.memoir_participant(id) on delete set null,  -- ON DELETE: guess
  created_at                  timestamptz not null default now(),
  expires_at                  timestamptz,
  revoked_at                  timestamptz,
  open_count                  integer not null default 0,
  -- Added by the reader-password fix (src/domain/share_service.py):
  visibility                  text not null default 'password'
                               check (visibility in ('private', 'link', 'password')),
  password_hash                text
);

create table public.memoir_export (
  id                              uuid primary key default gen_random_uuid(),
  memoir_id                       uuid not null references public.memoir(id) on delete cascade,  -- ON DELETE: guess
  kind                             public.export_kind not null,
  status                           public.job_status not null default 'queued',
  requested_by_participant_id     uuid not null,  -- live has no FK here either
  storage_key                     text,
  byte_size                       bigint,
  error_message                   text,
  expires_at                      timestamptz,
  created_at                      timestamptz not null default now(),
  completed_at                    timestamptz
);

-- ============================================================================
-- 4. Indexes (best-effort — not confirmed against the shared project)
-- ============================================================================
create index if not exists idx_media_asset_memoir_id on public.media_asset(memoir_id);
create index if not exists idx_memory_memoir_id on public.memory(memoir_id);
create index if not exists idx_comment_memoir_id on public.comment(memoir_id);
create index if not exists idx_comment_memory_id on public.comment(memory_id);
create index if not exists idx_memoir_participant_memoir_id on public.memoir_participant(memoir_id);
create index if not exists idx_memoir_participant_user_id on public.memoir_participant(user_id);
create index if not exists idx_memoir_link_token on public.memoir_link(token);
create index if not exists idx_memoir_link_memoir_id on public.memoir_link(memoir_id);
create index if not exists idx_memoir_export_memoir_id on public.memoir_export(memoir_id);
create index if not exists idx_media_asset_caption_tsv on public.media_asset using gin(caption_tsv);
create index if not exists idx_memory_search_tsv on public.memory using gin(search_tsv);
create index if not exists idx_transcript_search_tsv on public.transcript using gin(search_tsv);
