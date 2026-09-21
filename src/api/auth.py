"""
@file api/routers/auth.py
@description FastAPI router for user registration and authentication endpoints.
"""

from fastapi import APIRouter, Request, Response, status
from src.core.config import (
    ACCESS_TOKEN_COOKIE_MAX_AGE,
    ACCESS_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_MAX_AGE,
    REFRESH_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_PATH,
    settings,
)
from src.schemas.auth import UserRegisterRequest, UserLoginRequest # Import login request schema
from src.domain.auth_service import AuthService

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _set_access_token_cookie(response: Response, access_token: str) -> None:
    """
    Issues the session as an httpOnly cookie -- frontend JS never sees or
    handles this token at all, which is the actual point of moving off
    localStorage (XSS can't read an httpOnly cookie the way it can read
    localStorage or a plain cookie).
    """
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
        max_age=ACCESS_TOKEN_COOKIE_MAX_AGE,
    )


def _set_refresh_token_cookie(response: Response, refresh_token: str) -> None:
    """
    Issues the refresh token as its own httpOnly cookie, scoped to /api/auth
    only -- it never needs to leave that path, unlike the access token which
    every API route needs. Lets a session survive past the 1-hour access
    token via POST /api/auth/refresh instead of forcing a fresh /login.
    """
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=REFRESH_TOKEN_COOKIE_PATH,
        max_age=REFRESH_TOKEN_COOKIE_MAX_AGE,
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def register_user_endpoint(payload: UserRegisterRequest, response: Response):
    """
    Registers a new user account and triggers automatic database account
    provisioning. If Supabase email confirmation is disabled, this also logs
    the user in immediately (httpOnly cookie); otherwise the caller must
    confirm their email before /login will succeed.
    """
    result = AuthService.register_user(payload)
    user_data = result.get("user", {})
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")

    if access_token:
        _set_access_token_cookie(response, access_token)
    if refresh_token:
        _set_refresh_token_cookie(response, refresh_token)

    return {
        "success": True,
        "message": result["message"],
        "requires_confirmation": result.get("requires_confirmation", False),
        # Never the raw token -- just whether a session was actually started,
        # so the frontend can branch without ever touching the token itself.
        "authenticated": bool(access_token),
        "data": {
            "user_id": user_data.get("id"),
            "email": user_data.get("email"),
            "full_name": user_data.get("full_name"),
        }
    }


@router.post("/login", status_code=status.HTTP_200_OK)
def login_user_endpoint(payload: UserLoginRequest, response: Response):
    """Authenticates an existing user and starts their session via an httpOnly cookie."""
    result = AuthService.login_user(payload)
    _set_access_token_cookie(response, result["access_token"])
    if result.get("refresh_token"):
        _set_refresh_token_cookie(response, result["refresh_token"])

    return {
        "success": True,
        "message": result.get("message", "Login successful"),
        "authenticated": True,
        "data": {
            "user_id": result.get("user_id"),
            "email": result.get("email"),
            "full_name": result.get("full_name"),
            "active_memoir": result.get("active_memoir") # Optional: if fetched during login
        }
    }


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh_session_endpoint(request: Request, response: Response):
    """
    Exchanges the httpOnly refresh-token cookie for a new access token (and a
    rotated refresh token), so a session can outlive the 1-hour access-token
    cookie without the user losing in-progress work and having to sign in
    again. The frontend calls this on a 401 (or proactively before the access
    token's known expiry) instead of redirecting straight to /login.
    """
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    result = AuthService.refresh_session(refresh_token)

    _set_access_token_cookie(response, result["access_token"])
    if result.get("refresh_token"):
        _set_refresh_token_cookie(response, result["refresh_token"])

    return {
        "success": True,
        "message": result.get("message", "Session refreshed."),
        "authenticated": True,
        "data": {
            "user_id": result.get("user_id"),
            "email": result.get("email"),
        }
    }


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout_user_endpoint(response: Response):
    """
    Clears the session cookies. This is the only way to end a session now that
    the tokens are httpOnly -- frontend JS has no way to delete them itself.
    """
    response.delete_cookie(key=ACCESS_TOKEN_COOKIE_NAME, path="/")
    response.delete_cookie(key=REFRESH_TOKEN_COOKIE_NAME, path=REFRESH_TOKEN_COOKIE_PATH)
    return {"success": True, "message": "Logged out."}
