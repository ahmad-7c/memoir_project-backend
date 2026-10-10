import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from passlib.context import CryptContext

from src.core.reader_auth import READER_TOKEN_TTL_SECONDS, issue_reader_token
from src.integrations import memoir_repository
from src.integrations.share_repository import ShareRepository

# bcrypt via passlib, per spec — password hashes are never stored or returned in plaintext.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ShareService:

    @staticmethod
    async def create_or_get_share_link(memoir_id: str, user_id: str, scope: str = "view"):
        # 404, not 403: the predicate (role == 'owner') is part of the query
        # itself (get_owner_participant), so "not yours" and "doesn't exist"
        # are already indistinguishable by the time we get here -- no
        # separate branch to accidentally get wrong.
        participant = await ShareRepository.get_owner_participant(memoir_id, user_id)
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        memoir = await ShareRepository.get_memoir_by_id(memoir_id)
        if not memoir or memoir.get("status") != "published":
            raise HTTPException(status_code=409, detail="Publish this memoir before sharing it.")

        existing = await ShareRepository.get_active_link(memoir_id, scope)
        if existing:
            return existing

        # 'password' is the safe default: with no password_hash set yet, the link is
        # inert (unlock always fails closed) until the owner explicitly sets a
        # password or downgrades visibility to 'link'. Token generated with
        # `secrets`, never `random` or uuid4 -- see memoir_service.publish_memoir's
        # own token generation for the full rationale; this path covers the
        # (owner-initiated, not atomic-publish) create/rotate case.
        insert_data = {
            "memoir_id": memoir_id,
            "scope": scope,
            "token": secrets.token_urlsafe(32),
            "visibility": "password",
            "created_by_participant_id": participant["id"]
        }
        return await ShareRepository.insert_link(insert_data)

    @staticmethod
    async def get_share_link(memoir_id: str, user_id: str, scope: str = "view"):
        """Read-only retrieval for the owner's UI -- never creates one."""
        participant = await ShareRepository.get_owner_participant(memoir_id, user_id)
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        link = await ShareRepository.get_active_link(memoir_id, scope)
        if not link:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active share link.")
        return link

    @staticmethod
    async def update_share_link(memoir_id: str, user_id: str, payload, scope: str = "view"):
        participant = await ShareRepository.get_owner_participant(memoir_id, user_id)
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        link = await ShareRepository.get_active_link(memoir_id, scope)
        if not link:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active share link.")

        update_data = {}
        if payload.expires_at is not None:
            update_data["expires_at"] = payload.expires_at.isoformat()

        if payload.clear_password:
            update_data["password_hash"] = None
        elif payload.password is not None:
            update_data["password_hash"] = pwd_context.hash(payload.password)

        if payload.visibility is not None:
            update_data["visibility"] = payload.visibility

        # "A toggle for commenting" is a memoir-level setting
        # (comment_policy), not a column on the link row -- but the owner's
        # one control surface for "the sharing experience" is this endpoint,
        # so it's exposed here rather than requiring a second API call.
        if payload.can_comment is not None:
            # memoir_repository functions are plain sync defs (supabase-py is
            # synchronous under the hood) -- not awaited, matching every
            # other call into that module from this async method.
            memoir_repository.update_memoir_comment_policy(
                memoir_id, "anyone_who_can_view" if payload.can_comment else "invited_only"
            )

        if not update_data:
            return link

        return await ShareRepository.update_link(link["id"], update_data)

    @staticmethod
    async def revoke_share_link(memoir_id: str, user_id: str, scope: str = "view"):
        participant = await ShareRepository.get_owner_participant(memoir_id, user_id)
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

        link = await ShareRepository.get_active_link(memoir_id, scope)
        if not link:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active share link.")

        await ShareRepository.update_link(link["id"], {"revoked_at": datetime.now(timezone.utc).isoformat()})

    @staticmethod
    async def resolve_live_link(token: str) -> Dict[str, Any]:
        """
        The one place that decides whether a share link is currently usable
        at all. Not found, revoked, private, expired, and "points at a
        memoir that isn't published" are all the SAME failure from the
        outside (see unlock_share_link below for why that matters) -- and
        this is called both at unlock time AND on every subsequent
        reader-authenticated request (see domain/reader_access.py), so
        revoking a link takes effect on the reader's very next request
        rather than only once their existing 24h token happens to expire.

        Returns {"link": ..., "memoir": ...}. Raises 404 otherwise.
        """
        link = await ShareRepository.get_link_by_token(token)
        if not link or link.get("revoked_at") or link.get("visibility") == "private":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer available.")

        if link.get("expires_at") and datetime.now(timezone.utc) > _parse_timestamp(link["expires_at"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer available.")

        memoir = await ShareRepository.get_memoir_by_id(link["memoir_id"])
        if not memoir or memoir.get("status") != "published":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer available.")

        return {"link": link, "memoir": memoir}

    @staticmethod
    async def unlock_share_link(token: str, display_name: str, password: str) -> dict:
        """
        Verifies a reader's name (and the link's password, unless visibility is
        'link') and issues a short-lived, self-contained reader token. Not-found,
        revoked, unpublished, and private-visibility links are all indistinguishable
        404s from the outside; only a genuinely wrong password on a live link gets
        the generic 401 — never which part was wrong.
        """
        resolved = await ShareService.resolve_live_link(token)
        link = resolved["link"]
        memoir = resolved["memoir"]

        visibility = link.get("visibility") or "password"
        if visibility != "link":
            password_hash = link.get("password_hash")
            try:
                password_ok = bool(password_hash) and pwd_context.verify(password or "", password_hash)
            except Exception:
                password_ok = False
            if not password_ok:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")

        can_comment = memoir.get("comment_policy") == "anyone_who_can_view"

        reader_token = issue_reader_token(
            share_link_id=str(link["id"]),
            memoir_id=str(link["memoir_id"]),
            display_name=display_name,
            can_comment=can_comment,
        )

        await ShareRepository.increment_open_count(link["id"], link.get("open_count", 0))

        return {
            "access_token": reader_token,
            "token_type": "bearer",
            "expires_in": READER_TOKEN_TTL_SECONDS,
            "display_name": display_name,
            "can_comment": can_comment,
        }

    @staticmethod
    async def get_published_memoir(memoir_id: str) -> Optional[dict]:
        memoir = await ShareRepository.get_memoir_by_id(memoir_id)
        if not memoir or memoir.get("status") != "published":
            return None
        return memoir
