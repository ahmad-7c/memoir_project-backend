"""memoir immutability trigger

Revision ID: d8b7b48a10ea
Revises: 7b1f8f293d3e
Create Date: 2026-09-18 00:36:47.679108

Feature Request 04: a published memoir is immutable. Comments are the only
thing that may still be added after publication.

This is layer 2 of two (see src/domain/authorization.py::assert_memoir_editable
for layer 1, the application-level guard). Layer 1 depends on every write path
remembering to call it; this trigger does not — it fires regardless of which
code path, client, or hand-run query performed the write.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8b7b48a10ea'
down_revision: Union[str, None] = '7b1f8f293d3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
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
    """)

    for table in ("memory", "media_asset", "memory_media", "transcript"):
        op.execute(f"drop trigger if exists trg_{table}_immutable on public.{table};")
        op.execute(f"""
            create trigger trg_{table}_immutable
              before insert or update or delete on public.{table}
              for each row execute function public.prevent_writes_to_published_memoir();
        """)


def downgrade() -> None:
    for table in ("memory", "media_asset", "memory_media", "transcript"):
        op.execute(f"drop trigger if exists trg_{table}_immutable on public.{table};")
    op.execute("drop function if exists public.prevent_writes_to_published_memoir();")
