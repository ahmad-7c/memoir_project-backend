"""
@file src/core/config.py
@description Centralized application configuration and environment variable validation
using Pydantic BaseSettings.
"""

from typing import List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded securely from environment variables.
    """
    supabase_url: str = Field(..., validation_alias="SUPABASE_URL")
    supabase_anon_key: str = Field(..., validation_alias="SUPABASE_ANON_KEY")
    supabase_secret_key: str = Field(..., validation_alias="SUPABASE_SERVICE_ROLE_KEY")
    database_url: str = Field(..., validation_alias="DATABASE_URL")
    supabase_jwks_url: str = Field(..., validation_alias="SUPABASE_JWKS_URL")
    supabase_media_bucket: str = Field("media", validation_alias="SUPABASE_BUCKET_NAME")

    media_max_bytes: int = Field(52_428_576, validation_alias="MEDIA_MAX_BYTES")
    media_signed_url_ttl: int = Field(300, validation_alias="MEDIA_SIGNED_URL_TTL")

    # Secret used to sign/verify short-lived reader (name+password) tokens.
    # Deliberately NOT the Supabase JWKS/secret — readers never touch owner auth.
    reader_jwt_secret: str = Field(..., validation_alias="READER_JWT_SECRET")

    share_link_base_url: str = Field(
        "http://localhost:3000/share", validation_alias="SHARE_LINK_BASE_URL"
    )

    # No default — a misconfigured deployment must fail to start, not boot
    # looking healthy and only fail the first time someone records something.
    assemblyai_api_key: str = Field(..., validation_alias="ASSEMBLYAI_API_KEY")

    # Same rationale as assemblyai_api_key above — the reference AI-organization
    # branch read this via a bare os.getenv() inside the request path instead,
    # which only surfaces a missing key the first time someone clicks "Organize".
    gemini_api_key: str = Field(..., validation_alias="GEMINI_API_KEY")

    # Configurable so a model bump doesn't require a code change/redeploy,
    # and so it isn't repeated (and drift-able) across call sites.
    gemini_model: str = Field("gemini-3.6-flash", validation_alias="GEMINI_MODEL")


    # FIXED: Added cors_origins so main.py can dynamically read allowed origins from the environment
    cors_origins: List[str] = Field(
        default=["http://localhost:3000", "http://127.0.0.1:3000"],
        validation_alias="CORS_ORIGINS"
    )

    # httpOnly access-token cookie. secure=False by default because local dev
    # runs over plain http://localhost -- flip COOKIE_SECURE=true in any real
    # deployment (https). samesite="lax" is correct for frontend/backend on
    # different ports of the same host (localhost:3000 -> localhost:8000 is
    # still "same-site" under the SameSite spec, which ignores port); a
    # deployment on genuinely different domains would need "none" + secure=true.
    cookie_secure: bool = Field(False, validation_alias="COOKIE_SECURE")
    cookie_samesite: str = Field("lax", validation_alias="COOKIE_SAMESITE")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | List[str]) -> List[str]:
        """
        Parses comma-separated CORS origins string from environment variables into a list,
        or accepts an existing list.
        """
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, list):
            return v
        return ["http://localhost:3000"]

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"
    )


settings = Settings()

# Storage tiers for media asset lifecycle management
STORAGE_TIER_HOT = "hot"
STORAGE_TIER_COLD = "cold"

# Transcription status states for audio/video assets. Must match the live
# public.transcode_status Postgres enum on media_asset.transcription_status
# exactly (pending, processing, ready, failed, skipped) — these are unused
# elsewhere yet, so correcting them here doesn't change any behavior.
TRANSCRIPTION_STATUS_PENDING = "pending"
TRANSCRIPTION_STATUS_PROCESSING = "processing"
TRANSCRIPTION_STATUS_READY = "ready"
TRANSCRIPTION_STATUS_FAILED = "failed"
TRANSCRIPTION_STATUS_SKIPPED = "skipped"

# memoir.video_bytes_cap is NOT NULL with no database default; this is the
# platform default applied at memoir creation (not currently owner-configurable).
DEFAULT_VIDEO_BYTES_CAP = 5 * 1024 * 1024 * 1024  # 5 GiB

SUPABASE_JWKS_URL = settings.supabase_jwks_url

# Name of the httpOnly cookie carrying the Supabase access token, and its
# max-age -- matches Supabase's default JWT lifetime (1 hour) so the cookie
# doesn't outlive the token it holds.
ACCESS_TOKEN_COOKIE_NAME = "access_token"
ACCESS_TOKEN_COOKIE_MAX_AGE = 3600

# httpOnly refresh-token cookie. Supabase refresh tokens are rotated on each
# use and aren't tied to the access token's 1-hour lifetime -- 30 days gives
# a browser session a normal "stay signed in" lifetime without the access
# token itself living that long. Scoped to /api/auth only (not "/") since
# it only ever needs to reach the login/refresh/logout endpoints, unlike the
# access token cookie which every API route needs.
REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
REFRESH_TOKEN_COOKIE_PATH = "/api/auth"