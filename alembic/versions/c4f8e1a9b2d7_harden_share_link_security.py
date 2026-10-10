r"""extend immutability trigger to chapter/narrative_section, block share
links for unpublished memoirs at the database layer, and let a comment
target a narrative section

Revision ID: c4f8e1a9b2d7
Revises: b7d2a93f4c1e
Create Date: 2026-10-10 00:00:00.000000

Three independent hardenings, all database-layer backstops for rules the
application layer already enforces -- the whole point of a layer 2 is that
it does not depend on every write path remembering to call the layer 1
guard, or even going through the API at all.

1. `chapter` and `narrative_section` were added (migrations c3f9a1b204d7 and
   b7d2a93f4c1e) after the original immutability trigger (d8b7b48a10ea), so
   neither ever got the trigger. The AI organization/narrative feature can
   currently rewrite a published memoir's chapters and narrative text
   straight through Postgres, bypassing every assert_memoir_editable() call
   in the Python layer entirely. Reuses the existing
   prevent_writes_to_published_memoir() function (already updated by
   d8ebd28b0d20) rather than redefining it -- both tables carry a plain
   `memoir_id` column with no special-case columns that function's narrow
   transcription-pipeline exemption needs to know about.

2. `memoir_link` had no database-level guard at all: application code
   (ShareService.create_or_get_share_link) checks memoir.status ==
   'published' before inserting, but nothing stopped a hand-run INSERT (or a
   future code path that forgets the check) from creating a working share
   link to a memoir still being edited. New dedicated trigger, INSERT only
   -- UPDATE must stay allowed unconditionally, because revoking a link
   (setting revoked_at) must always be possible regardless of the memoir's
   current status.

3. `comment.narrative_section_id`: comments can now attach to a specific
   narrative section (e.g. commenting on the AI-composed prose itself) as an
   alternative to `memory_id`. Nullable, same pattern as the existing
   nullable memory_id/media_asset_id -- a comment still needs at least one
   target, enforced in the application layer where the richer "not more than
   one target" rule can live.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c4f8e1a9b2d7"
down_revision: Union[str, None] = "b7d2a93f4c1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. Immutability trigger coverage ---------------------------------
    for table in ("chapter", "narrative_section"):
        op.execute(f"drop trigger if exists trg_{table}_immutable on public.{table};")
        op.execute(
            f"""
            create trigger trg_{table}_immutable
              before insert or update or delete on public.{table}
              for each row execute function public.prevent_writes_to_published_memoir();
            """
        )

    # --- 2. A share link cannot be created for an unpublished memoir ------
    op.execute(
        """
        create or replace function public.prevent_share_link_for_unpublished_memoir()
        returns trigger
        language plpgsql
        security definer
        set search_path = public
        as $$
        declare
          v_status public.memoir_status;
        begin
          select status into v_status from public.memoir where id = new.memoir_id;

          if v_status is distinct from 'published' then
            raise exception 'Cannot create a share link for a memoir that has not been published.'
              using errcode = 'P0005';
          end if;

          return new;
        end;
        $$;
        """
    )
    op.execute("drop trigger if exists trg_memoir_link_requires_published on public.memoir_link;")
    op.execute(
        """
        create trigger trg_memoir_link_requires_published
          before insert on public.memoir_link
          for each row execute function public.prevent_share_link_for_unpublished_memoir();
        """
    )

    # --- 3. Comments may target a narrative section ------------------------
    op.add_column(
        "comment",
        sa.Column(
            "narrative_section_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "comment_narrative_section_id_fkey",
        "comment",
        "narrative_section",
        ["narrative_section_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_comment_narrative_section_id", "comment", ["narrative_section_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_comment_narrative_section_id", table_name="comment")
    op.drop_constraint("comment_narrative_section_id_fkey", "comment", type_="foreignkey")
    op.drop_column("comment", "narrative_section_id")

    op.execute("drop trigger if exists trg_memoir_link_requires_published on public.memoir_link;")
    op.execute("drop function if exists public.prevent_share_link_for_unpublished_memoir();")

    for table in ("chapter", "narrative_section"):
        op.execute(f"drop trigger if exists trg_{table}_immutable on public.{table};")
