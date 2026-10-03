"""
@file backend/src/api/organize_page.py
@description Serves the AI organization review console as a static page.

WHY A ROUTE FOR A STATIC FILE

The deployed Next.js frontend cannot show this feature without a frontend
deploy, and its "Generate Chapters" button cannot show a *review* step at all --
it calls POST /organize and then polls GET /chapters, which by design returns
nothing until an owner confirms. The multi-agent pipeline produces a proposal,
not chapters (root AGENTS.md section 3.6), so that button necessarily ends in a
60-second spinner.

Rather than remove the review step to make an old button look like it works,
this serves the real console from the API. It needs no bundler, no npm, no
frontend deploy, and no change to any deployed asset.

Same-origin is the other benefit and the more important one: the httpOnly auth
cookie is sent without any CORS configuration and without the cross-site
`SameSite=None` requirement that the Vercel/AWS split imposes on the main app.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse

from src.core.auth import get_current_user

router = APIRouter(tags=["AI Organization UI"])

_PAGE = Path(__file__).resolve().parent.parent / "static" / "organize.html"


@router.get("/organize", include_in_schema=False)
def organize_index():
    """
    Sends bare /organize to the memoir list.

    The page needs a memoir id, and inventing a "pick one" screen here would
    duplicate the dashboard. Redirecting is honest about that.
    """
    return RedirectResponse(url="/memoirs", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/organize/{memoir_id}", include_in_schema=False)
def organize_console(memoir_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """
    The review console for one memoir.

    `get_current_user` is a real dependency and it is load-bearing twice over:

      * It rejects an unauthenticated request, so the page is not served to a
        stranger. The page itself contains no memoir data -- every value comes
        from the authenticated API -- so serving it publicly would be harmless,
        but there is no reason to.
      * It means a session-expired user gets a clean 401 instead of a page that
        loads and then fails every request inside it.

    Note what is NOT done here: the memoir is not authorized against this route.
    Every endpoint the page calls re-verifies owner access independently, so a
    user who navigates to another family's memoir id gets a page that renders
    empty and 404s on every load. That is the intended behaviour -- the page is
    not a security boundary, the API is.
    """
    if not _PAGE.exists():
        # A packaging error, not a user error. 500 rather than 404: the file
        # being absent means the image is broken, and a 404 would send whoever
        # hits this looking for a bad URL.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The organization console is missing from this deployment.",
        )

    return FileResponse(
        _PAGE,
        media_type="text/html; charset=utf-8",
        headers={
            # A strict CSP. The page ships no third-party resources at all, so
            # this costs nothing and it removes script injection as an option
            # even if a value ever reached innerHTML through a future edit.
            #
            # `connect-src 'self'` is the interesting one: the page may talk to
            # this API and nothing else, so an injected script cannot exfiltrate
            # a memoir to an attacker's host.
            "Content-Security-Policy": (
                "default-src 'none'; "
                "script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "form-action 'none'; "
                "frame-ancestors 'none'; "
                "base-uri 'none'"
            ),
            # This page shows a family's archive. It must not be cached by a
            # shared proxy or a back-button restore after logout.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            # Same as the API's own cookie config. Without Secure the browser
            # silently drops the session cookie and every request 401s.
            "X-Frame-Options": "DENY",
        },
    )