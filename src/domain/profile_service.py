"""
@file domain/profile_service.py
@description Business logic for the owner profile page: account details plus
every memoir the caller owns, ordered drafts-first (they need attention) then
published, newest first within each group.
"""

from typing import Any, Dict, List

from fastapi import HTTPException, status

from src.integrations import profile_repository


class ProfileService:

    @staticmethod
    def get_owner_profile(user_id: str) -> Dict[str, Any]:
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")

        account = profile_repository.fetch_account(user_id)
        if not account:
            # Mirrors MemoirService.create_memoir's self-heal rationale: the
            # auth.users -> user_account provisioning trigger doesn't reliably
            # fire for every signup, so a brand-new account can legitimately
            # have no row yet. A profile page is read-only, so there is
            # nothing to self-heal into here -- surface it plainly instead.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

        memoirs = profile_repository.fetch_owned_memoirs(user_id)
        memoir_ids = [str(m["id"]) for m in memoirs]
        memory_counts = profile_repository.fetch_memory_counts(memoir_ids)

        cover_media_ids = [str(m["cover_media_id"]) for m in memoirs if m.get("cover_media_id")]
        cover_urls = profile_repository.fetch_cover_image_urls(cover_media_ids)

        summaries = []
        for m in memoirs:
            mid = str(m["id"])
            cover_media_id = m.get("cover_media_id")
            summaries.append(
                {
                    "id": mid,
                    "subject_name": m.get("subject_name"),
                    "subject_born_on": m.get("subject_born_on"),
                    "subject_died_on": m.get("subject_died_on"),
                    "subject_is_living": bool(m.get("subject_is_living")),
                    "cover_image_url": cover_urls.get(str(cover_media_id)) if cover_media_id else None,
                    "status": m.get("status"),
                    "published_at": m.get("published_at"),
                    "memory_count": memory_counts.get(mid, 0),
                    "updated_at": m.get("updated_at"),
                }
            )

        # Drafts first (they need attention), then published; newest first
        # within each group -- "newest" is the most recent edit for a draft
        # (updated_at) and the most recent publish for a published memoir
        # (published_at), since that's the timestamp that actually changed
        # last for each group.
        summaries.sort(key=lambda s: (s["status"] == "published", _negated_timestamp(s)))

        return {
            "name": account.get("full_name"),
            "email": account.get("email"),
            "subscription_status": account.get("subscription_status") or "free",
            "memoirs": summaries,
        }


def _negated_timestamp(summary: Dict[str, Any]) -> float:
    """
    Sort helper: higher (more recent) timestamps must sort first within a
    status group, so this returns the negative epoch seconds -- `sort()` only
    supports ascending order, and reversing per-group isn't an option since
    both groups are sorted in a single pass.
    """
    from datetime import datetime, timezone

    raw = summary["published_at"] if summary["status"] == "published" else summary["updated_at"]
    if not raw:
        return 0.0
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return -raw.timestamp()
