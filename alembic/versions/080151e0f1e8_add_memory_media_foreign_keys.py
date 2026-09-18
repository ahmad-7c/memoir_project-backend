"""add memory_media foreign keys

Revision ID: 080151e0f1e8
Revises: 879e2c8d1b8c
Create Date: 2026-09-18 00:54:34.149841

memory_media.memory_id and .media_asset_id had no FK constraints at all in the
live schema — just plain uuid columns forming the composite PK. PostgREST
can't resolve the memory_media(media_asset(*)) embedded-select the memory feed
depends on without a real FK, so GET /api/memories/feed/{memoir_id} 500s
whenever a memory actually has attached media. Adding the FKs both fixes the
feed and closes an integrity gap (junction rows could point at deleted
memories/assets).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '080151e0f1e8'
down_revision: Union[str, None] = '879e2c8d1b8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "memory_media_memory_id_fkey", "memory_media", "memory",
        ["memory_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "memory_media_media_asset_id_fkey", "memory_media", "media_asset",
        ["media_asset_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("memory_media_media_asset_id_fkey", "memory_media", type_="foreignkey")
    op.drop_constraint("memory_media_memory_id_fkey", "memory_media", type_="foreignkey")
