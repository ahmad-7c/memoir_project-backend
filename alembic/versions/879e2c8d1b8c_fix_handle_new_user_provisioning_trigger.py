"""fix handle_new_user provisioning trigger

Revision ID: 879e2c8d1b8c
Revises: d8b7b48a10ea
Create Date: 2026-09-18 00:37:04.759590

The auth.users -> public.user_account provisioning trigger predates this
migration history (it shipped with the project). Two bugs against the current
schema:
  1. `on conflict (auth_provider_uid)` with no unique constraint on that column
     -> every signup failed with "Database error saving new user".
  2. full_name was never set, and the column is NOT NULL with no default.

Written idempotently (safe to re-run) since this was already hotfixed by hand
against the live sandbox before this migration existed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '879e2c8d1b8c'
down_revision: Union[str, None] = 'd8b7b48a10ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        do $$ begin
          alter table public.user_account
            add constraint user_account_auth_provider_uid_key unique (auth_provider_uid);
        exception when duplicate_table then null; end $$;
    """)

    op.execute("""
        create or replace function public.handle_new_user()
        returns trigger
        language plpgsql
        security definer
        set search_path to 'public'
        as $function$
        begin
          insert into public.user_account (id, email, full_name, auth_provider_uid, created_at)
          values (
            new.id,
            new.email,
            coalesce(new.raw_user_meta_data->>'full_name', split_part(new.email, '@', 1)),
            new.id::text,
            now()
          )
          on conflict (auth_provider_uid) do nothing;
          return new;
        end;
        $function$;
    """)


def downgrade() -> None:
    # Not reverting to the broken version — nothing meaningful to downgrade to.
    pass
