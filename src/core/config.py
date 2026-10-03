"""
@file src/core/config.py
@description Centralized application configuration and environment variable validation
using Pydantic BaseSettings.
"""

from typing import Annotated, List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # ---------------------------------------------------------------------
    # AI provider chains
    #
    # Groq is the PRIMARY for AI organization (fast, cheap, and supports
    # Structured Outputs in strict mode -- constrained decoding, so the JSON
    # shape is guaranteed rather than merely attempted). Gemini is the
    # FALLBACK: already integrated, so a Groq outage degrades latency/cost
    # rather than taking the feature down.
    #
    # Fallback providers are Optional because a deployment may legitimately
    # run with only one -- but a chain with ZERO usable providers is a
    # misconfiguration and validate_ai_provider_chain() refuses to boot.
    # A missing fallback key must fail loudly at startup, not silently
    # degrade into a 500 on a user's request an hour later.
    # ---------------------------------------------------------------------
    groq_api_key: Optional[str] = Field(None, validation_alias="GROQ_API_KEY")
    groq_organize_model: str = Field("openai/gpt-oss-120b", validation_alias="GROQ_ORGANIZE_MODEL")

    # Deepgram is the transcription FALLBACK, deliberately not Groq/Whisper:
    # Whisper hallucinates text during silence, music and crosstalk. Fabricated
    # sentences in a dead relative's recording is a worse failure than a
    # transcription outage. AssemblyAI also reports ~30% fewer hallucinations,
    # and Deepgram is cheaper per hour and faster. A fallback is not
    # automatically a good fallback.
    deepgram_api_key: Optional[str] = Field(None, validation_alias="DEEPGRAM_API_KEY")
    deepgram_model: str = Field("nova-3", validation_alias="DEEPGRAM_MODEL")

    # Per-attempt timeout. Generous enough for a long context, tight enough
    # that a hung upstream cannot pin a threadpool slot for the whole
    # Starlette pool -- with a single-shot provider that is a full outage.
    ai_request_timeout_seconds: float = Field(30.0, validation_alias="AI_REQUEST_TIMEOUT_SECONDS")

    # Attempts against ONE provider before failing over to the next. >1 so a
    # transient 429 doesn't cost us a failover, but bounded so a dead provider
    # can't multiply latency across the whole chain.
    ai_max_attempts_per_provider: int = Field(2, validation_alias="AI_MAX_ATTEMPTS_PER_PROVIDER")

    # Caps concurrent agent calls. Providers rate-limit on RPM/TPM, so an
    # unbounded asyncio.gather is a self-inflicted 429.
    ai_max_concurrency: int = Field(4, validation_alias="AI_MAX_CONCURRENCY")

    # Circuit breaker: after N consecutive provider failures, short-circuit
    # for a cooldown so a dead provider stops adding its timeout to every
    # subsequent request instead of failing over on every single call.
    ai_circuit_failure_threshold: int = Field(3, validation_alias="AI_CIRCUIT_FAILURE_THRESHOLD")
    ai_circuit_cooldown_seconds: int = Field(60, validation_alias="AI_CIRCUIT_COOLDOWN_SECONDS")

    # Wall-clock ceiling for a whole multi-stage organize pipeline. A job past
    # this is a bug or a pathological memoir, not a slow success.
    organize_pipeline_deadline_seconds: int = Field(180, validation_alias="ORGANIZE_PIPELINE_DEADLINE_SECONDS")

    # FIXED: Added cors_origins so main.py can dynamically read allowed origins
    # from the environment.
    #
    # `NoDecode` is load-bearing, not decoration. pydantic-settings treats a
    # `List[str]` environment variable as JSON and JSON-parses it *before* the
    # field validator below ever runs, so the documented comma-separated form
    #
    #     CORS_ORIGINS=https://a.example,https://b.example
    #
    # raises `SettingsError: error parsing value for field "cors_origins"` and
    # the process refuses to boot. The only value that worked was a literal JSON
    # array, which nobody would guess from the docs.
    #
    # This went unnoticed because local development never set the variable: the
    # default list applied and the parsing path was never exercised. It surfaced
    # the first time the value came from a real environment -- which in
    # production is a deploy, not a `npm run dev`.
    #
    # `NoDecode` tells pydantic-settings to hand the raw string to the validator
    # instead, so the comma-separated form documented here is the form that
    # works. A JSON array still parses, because the validator accepts one.
    cors_origins: Annotated[
        List[str],
        NoDecode,
    ] = Field(
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
        Accepts a comma-separated string, a literal JSON array, or a real list.

        The comma form is what the deployment docs and `.env.production.example`
        tell operators to write, and it is what a human produces under time
        pressure. The JSON form is what pydantic-settings would otherwise have
        demanded. Both work, so neither audience is punished.

        Entries are stripped and empties dropped: a trailing comma in
        `CORS_ORIGINS=https://a.example,` would otherwise produce an empty origin
        that matches nothing while still looking configured.
        """
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return []
            if text.startswith("["):
                import json

                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    # Not JSON after all -- fall through to comma splitting
                    # rather than refusing to boot over a stray bracket.
                    parsed = None
                if isinstance(parsed, list):
                    return [str(o).strip() for o in parsed if str(o).strip()]
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v if str(origin).strip()]
        return ["http://localhost:3000"]

    @field_validator("cookie_samesite")
    @classmethod
    def normalize_samesite(cls, v: str) -> str:
        """Normalizes casing so downstream comparisons don't have to guess."""
        return v.strip().lower()

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"
    )


settings = Settings()


def validate_ai_provider_chain() -> None:
    """
    Refuses to boot if a *required* AI capability has zero usable providers.

    Individual provider keys are Optional (a deployment may run with only
    Groq and no Gemini, and that's legitimate). What is NOT legitimate is a
    chain with nothing in it — that turns a clear config error into an opaque
    500 the first time someone clicks "Organize", possibly in production,
    possibly hours after a deploy. Failing here costs five seconds at boot.

    Called at import time below, so `uvicorn src.main:app` refuses to start.
    Note this does NOT touch the network: it only checks which keys are
    present, so a boot-time call can't be slowed down or broken by a provider
    outage.
    """
    problems: List[str] = []

    if not settings.groq_api_key and not settings.gemini_api_key:
        problems.append(
            "AI organization has no LLM provider: set GROQ_API_KEY (primary) "
            "and/or GEMINI_API_KEY (fallback)."
        )

    if not settings.assemblyai_api_key:
        # Required at the Settings level already (no default), so this is
        # unreachable in practice -- kept as an explicit assertion because
        # transcription has no second mandatory provider to fall back to.
        problems.append("Transcription has no STT provider: set ASSEMBLYAI_API_KEY.")

    if problems:
        raise RuntimeError(
            "Refusing to start with an unusable AI provider chain:\n  - "
            + "\n  - ".join(problems)
        )


validate_ai_provider_chain()


def database_url_for_psycopg() -> str:
    """
    The configured Postgres URL in a form `psycopg.connect()` accepts.

    WHY THIS IS NEEDED

    `DATABASE_URL` carries a SQLAlchemy dialect prefix (`postgresql+psycopg://`)
    because Alembic requires one -- it is how SQLAlchemy knows which driver to
    load. `psycopg` has no concept of that syntax and rejects it outright:

        invalid connection option "postgresql+psycopg://..."

    Passing the configured value straight to `psycopg` therefore fails always,
    not intermittently. That made `/health/ready` return 503 on every
    environment, which is worse than not having the endpoint: it is a deploy
    gate, and a gate that is always red is a gate people learn to ignore.

    Kept next to `Settings` because it is a config-shape concern, not a database
    one, and because the alternative -- each caller remembering to strip the
    prefix -- is exactly the kind of omission that ships.
    """
    url = settings.database_url
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def validate_browser_delivery_settings() -> None:
    """
    Refuses to boot on cookie/CORS combinations that browsers silently reject.

    Both failure modes here are silent. Nothing errors, no request 500s, and
    every single authenticated call returns 401. They are also specific to the
    deployment shape this project is moving to: a Vercel-hosted frontend on one
    domain calling an AWS-hosted backend on another.

        SameSite=None without Secure
            Browsers discard the cookie outright. Login appears to succeed, then
            every subsequent request is unauthenticated. Setting COOKIE_SAMESITE=none
            without also setting COOKIE_SECURE=true is the exact mistake that turns
            a cross-domain deploy into an app nobody can log into.

        CORS_ORIGINS=* with credentials
            main.py sets allow_credentials=True because auth is an httpOnly cookie.
            A wildcard origin is illegal for credentialed requests, so the browser
            refuses the response. Same symptom, different cause.

    Cheap to check at boot. Expensive to diagnose from production logs.
    """
    problems: List[str] = []

    if settings.cookie_samesite == "none" and not settings.cookie_secure:
        problems.append(
            "COOKIE_SAMESITE=none requires COOKIE_SECURE=true. Browsers silently "
            "discard a SameSite=None cookie that is not Secure, which logs every "
            "user out immediately and presents as a backend bug."
        )

    if settings.cookie_samesite not in ("lax", "strict", "none"):
        problems.append(
            f"COOKIE_SAMESITE={settings.cookie_samesite!r} is not one of lax, strict, none."
        )

    if "*" in settings.cors_origins:
        problems.append(
            "CORS_ORIGINS must list explicit origins, not '*'. Authentication is "
            "a credentialed httpOnly cookie, and browsers reject credentialed "
            "responses that carry a wildcard origin."
        )

    if not settings.cors_origins:
        problems.append("CORS_ORIGINS is empty, so no browser origin can call this API.")

    if problems:
        raise RuntimeError(
            "Refusing to start with unusable browser-delivery settings:\n  - "
            + "\n  - ".join(problems)
        )


validate_browser_delivery_settings()

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