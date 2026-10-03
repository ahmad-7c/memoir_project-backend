"""
@file models/auth.py
@description Pydantic models for user authentication and registration requests.
"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field

class UserRegisterRequest(BaseModel):
    """
    extra="forbid": an unrecognized field on a registration body is either a
    client bug or something probing for accepted-but-ignored parameters. Both are
    worth a 422.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Upper bound added alongside the existing lower bound. A password field
    # with no maximum is an unbounded string hashed on every attempt, and
    # bcrypt only considers the first 72 bytes regardless -- so the limit also
    # documents where the real truncation happens.
    password: str = Field(
        ...,
        min_length=8,
        max_length=72,
        description="Password, 8-72 characters (bcrypt truncates beyond 72 bytes).",
    )
    full_name: str = Field(..., min_length=1, max_length=200, description="User's full name for profile setup.")

class UserLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Bounded for the same reason, though this one is never hashed.
    password: str = Field(..., min_length=1, max_length=72)