"""
@file auth_repository.py
@description Data access layer adapter module managing direct Supabase Auth 
sign-up/sign-in flows and user account profile table synchronization.
"""

from src.integrations.supabase_client import supabase


def auth_sign_up(email: str, password: str, full_name: str):
    """
    Invokes Supabase Auth registration to provision user credentials and metadata.

    Args:
        email (str): The user's email address.
        password (str): The user's password.
        full_name (str): The user's full name.

    Returns:
        Any: The Supabase auth response object.
    """
    return supabase.auth.sign_up({
        "email": email,
        "password": password,
        "options": {
            "email_redirect_to": "http://localhost:3000/login",
            "data": {
                "full_name": full_name
            }
        }
    })


def auth_sign_in(email: str, password: str):
    """
    Authenticates a user against Supabase Auth using email and password.

    Args:
        email (str): The user's email address.
        password (str): The user's password.

    Returns:
        Any: The Supabase auth response object containing session and user data.
    """
    return supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })


def auth_refresh_session(refresh_token: str):
    """
    Exchanges a still-valid refresh token for a new Supabase session (a new
    access_token, plus a rotated refresh_token).

    Args:
        refresh_token (str): The refresh token previously issued at login/signup.

    Returns:
        Any: The Supabase auth response object containing the new session and user data.
    """
    return supabase.auth.refresh_session(refresh_token)


def update_last_login(user_id: str, timestamp: str):
    """
    Updates the last login timestamp record for a specific user account.

    Args:
        user_id (str): The unique user ID.
        timestamp (str): The ISO-formatted UTC timestamp string.

    Returns:
        Any: The database query response object.
    """
    return supabase.table("user_account").update({
        "last_login_at": timestamp
    }).eq("id", user_id).execute()