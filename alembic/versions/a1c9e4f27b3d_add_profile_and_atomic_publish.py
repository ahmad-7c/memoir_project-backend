r"""add profile fields and atomic publish_memoir_tx() RPC

Revision ID: a1c9e4f27b3d
Revises: e7a41c5b9f32
Create Date: 2026-10-10 00:00:00.000000

Two independent additions for the owner profile page and the publish-to-share
feature:

1. `memoir.pdf_exportable` and `user_account.subscription_status` -- plain
   columns the new GET /me/profile endpoint reads directly.

2. `publish_memoir_tx(p_memoir_id, p_owner_user_id, p_token)` -- a Postgres
   function that does the whole publish operation (flip status, stamp
   published_at, flag pdf_exportable, create the share link) as ONE
   transaction, instead of three separate HTTP calls that could leave a
   memoir published with no share link if the process died in between.

   Supabase's REST client (postgrest) has no multi-statement transaction
   primitive over HTTP -- each `.execute()` is its own transaction. A single
   plpgsql function called via `.rpc()` is the only way to guarantee these
   writes commit or fail together. The function raises distinct SQLSTATEs
   (P0001-P0004) so the Python layer can map each failure to the right HTTP
   status without parsing English error text:

     P0001  memoir not found         -> 404
     P0002  caller is not the owner  -> 404 (never 403 -- see authorization.py)
     P0003  already published        -> 409 (publishing is irreversible)
     P0004  zero saved memories      -> 409 (nothing to publish)

   `security definer` is required because this needs to write to `memoir` and
   `memoir_link` from a function callable through the admin client's RPC path
   the same way the immutability trigger (migrations/d8b7b48a10ea) runs under
   the same model -- not a new privilege boundary, just consistent with it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c9e4f27b3d"
down_revision: Union[str, None] = "e7a41c5b9f32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memoir",
        sa.Column("pdf_exportable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "user_account",
        sa.Column("subscription_status", sa.Text(), nullable=False, server_default="free"),
    )
    op.create_check_constraint(
        "user_account_subscription_status_check",
        "user_account",
        "subscription_status in ('free', 'active', 'past_due', 'canceled')",
    )

    op.execute(
        """
        create or replace function public.publish_memoir_tx(
          p_memoir_id uuid,
          p_owner_user_id uuid,
          p_token text
        ) returns public.memoir
        language plpgsql
        security definer
        set search_path = public
        as $$
        declare
          v_memoir public.memoir;
          v_participant public.memoir_participant;
          v_memory_count integer;
          v_existing_link_id uuid;
        begin
          select * into v_memoir from public.memoir where id = p_memoir_id for update;
          if v_memoir.id is null then
            raise exception 'memoir_not_found' using errcode = 'P0001';
          end if;

          select * into v_participant from public.memoir_participant
            where memoir_id = p_memoir_id
              and user_id = p_owner_user_id
              and role = 'owner'
              and removed_at is null
            limit 1;
          if v_participant.id is null then
            raise exception 'not_owner' using errcode = 'P0002';
          end if;

          if v_memoir.status = 'published' then
            raise exception 'already_published' using errcode = 'P0003';
          end if;

          select count(*) into v_memory_count from public.memory
            where memoir_id = p_memoir_id
              and status = 'submitted'
              and deleted_at is null;
          if v_memory_count = 0 then
            raise exception 'no_memories' using errcode = 'P0004';
          end if;

          update public.memoir
             set status = 'published',
                 published_at = now(),
                 pdf_exportable = true,
                 updated_at = now()
           where id = p_memoir_id
          returning * into v_memoir;

          -- Reuse a still-live 'view' link if one somehow already exists
          -- (e.g. a retried request) instead of creating a second one.
          select id into v_existing_link_id from public.memoir_link
            where memoir_id = p_memoir_id and scope = 'view' and revoked_at is null
            limit 1;

          if v_existing_link_id is null then
            insert into public.memoir_link (memoir_id, scope, token, visibility, created_by_participant_id)
            values (p_memoir_id, 'view', p_token, 'password', v_participant.id);
          end if;

          return v_memoir;
        end;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists public.publish_memoir_tx(uuid, uuid, text);")
    op.drop_constraint("user_account_subscription_status_check", "user_account", type_="check")
    op.drop_column("user_account", "subscription_status")
    op.drop_column("memoir", "pdf_exportable")
