"""
@file src/db/models/_enums.py
@description Postgres enum types shared across models. create_type=False on every
one — these enums already exist in the database (created by
migrations/0000_bootstrap_sandbox_schema.sql); Alembic/SQLAlchemy must never try
to CREATE or DROP them as a side effect of a table migration.
"""

from sqlalchemy.dialects.postgresql import ENUM

authored_by = ENUM("contributor", "owner", "ai", name="authored_by", create_type=False)
media_kind = ENUM("audio", "photo", "video", name="media_kind", create_type=False)
storage_tier = ENUM("hot", "cold", name="storage_tier", create_type=False)
transcode_status = ENUM("pending", "processing", "ready", "failed", "skipped", name="transcode_status", create_type=False)
memoir_status = ENUM("draft", "published", name="memoir_status", create_type=False)
memoir_visibility = ENUM("invited_only", "link_with_password", "link_public", name="memoir_visibility", create_type=False)
comment_policy = ENUM("nobody", "invited_only", "anyone_who_can_view", name="comment_policy", create_type=False)
export_kind = ENUM("pdf", "raw_archive", name="export_kind", create_type=False)
job_status = ENUM("queued", "running", "ready", "failed", name="job_status", create_type=False)
link_scope = ENUM("contribute", "view", name="link_scope", create_type=False)
participant_role = ENUM("owner", "co_owner", "contributor", "reader", name="participant_role", create_type=False)
relationship_group = ENUM(
    "spouse_partner", "child", "grandchild", "sibling", "parent",
    "extended_family", "friend", "colleague", "neighbour", "self", "other",
    name="relationship_group", create_type=False,
)
notify_frequency = ENUM("instant", "weekly_digest", "off", name="notify_frequency", create_type=False)
memory_status = ENUM("draft", "submitted", name="memory_status", create_type=False)
date_precision = ENUM("day", "month", "year", "decade", name="date_precision", create_type=False)
media_link_type = ENUM("primary", "reference", name="media_link_type", create_type=False)
