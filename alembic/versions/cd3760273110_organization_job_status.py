"""organization job status

Revision ID: cd3760273110
Revises: 080151e0f1e8
Create Date: 2026-09-18 12:00:00.000000

The AI-organization feature (src/domain/organization_service.py) ran the
Gemini clustering job via BackgroundTasks but had nowhere to persist the
job's outcome -- a caller had no way to know it finished, failed, or died
with the API process (see migrations/0004_organization_job_status.sql,
which described this same change as a manual-SQL step; this migration is
that change folded into Alembic properly instead of staying untracked).

Mirrors the existing transcript retry pattern (migrations/0002,
src/domain/transcription_service.py): reuse the row the job is *about*
(here, the memoir) rather than a separate jobs table, and compute a
"stalled" presentation status at read time from organization_started_at
instead of storing it.

Reuses the existing public.job_status enum (queued/running/ready/failed)
already used by memoir_export -- no new enum needed.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'cd3760273110'
down_revision: Union[str, None] = '080151e0f1e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        alter table public.memoir
          add column if not exists organization_status public.job_status,
          add column if not exists organization_started_at timestamptz,
          add column if not exists organization_completed_at timestamptz,
          add column if not exists organization_error_message text;
    """)


def downgrade() -> None:
    op.execute("""
        alter table public.memoir
          drop column if exists organization_status,
          drop column if exists organization_started_at,
          drop column if exists organization_completed_at,
          drop column if exists organization_error_message;
    """)
