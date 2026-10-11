"""
@file main.py
@description Entry point for the FastAPI application. Bootstraps environment variables, 
configures CORS middleware for frontend integration, defines health check endpoints, 
and mounts all modular feature routers.
"""

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from dotenv import load_dotenv
from src.core.config import settings, database_url_for_psycopg
from src.api.share import owner_router, reader_router

# CRITICAL: load_dotenv() must be called BEFORE any other application modules
# are imported so database and storage configurations can read environment variables.
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.security_headers import SecurityHeadersMiddleware
from src.core.request_id import RequestIDMiddleware
from src.core.request_guards import RequestGuardMiddleware

# Import modular feature routers
from src.api.media import router as media_router
from src.api.memoir import router as memoir_router
from src.api.memory import router as memory_router
from src.api.auth import router as auth_router
from src.api.comments import router as comment_router
from src.api.search import router as search_router
from src.api.export import router as export_router
from src.api.transcripts import router as transcript_router
from src.api.organization import organization_router
from src.api.organize_page import router as organize_page_router
from src.api.profile import router as profile_router
from src.api.narrative import router as narrative_router

def setup_logging():
    """Configures root logging format and log level for backend services."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )


# Hard ceiling on the readiness probe's connection attempt. Long enough for a
# cold DNS lookup and a TLS handshake to Supabase, short enough that a wedged
# database cannot hold an event-loop worker for a noticeable fraction of a
# health-check interval.
READINESS_CONNECT_TIMEOUT_SECONDS = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager handling startup logging and shutdown events."""
    setup_logging()
    logging.info("Starting up Memoir backend application...")
    yield
    logging.info("Shutting down Memoir backend application...")


# Initialize the core FastAPI application instance
app = FastAPI(
    title="Memoir App API",
    version="1.0.0",
    description="Backend API services for the Memoir life-story documentation platform.",
    lifespan=lifespan
)

# Parse CORS origins dynamically (supporting both comma-separated strings and lists from settings)
origins = settings.cors_origins
if isinstance(origins, str):
    origins = [o.strip() for o in origins.split(",") if o.strip()]

# ---------------------------------------------------------------------------
# Middleware registration order matters and is a common source of silent
# security holes -- and Starlette's actual rule here is easy to get
# backwards. `Starlette.add_middleware` does `user_middleware.insert(0, ...)`,
# so each new call pushes earlier ones further from the front of that list;
# the stack is then built by wrapping in reverse over
# `[ServerErrorMiddleware] + user_middleware + [ExceptionMiddleware]`. Net
# effect: the LAST middleware added via add_middleware ends up OUTERMOST
# (right inside Starlette's own ServerErrorMiddleware), not the first. So to
# get, outermost to innermost:
#
#   1. Security headers (outermost -- must apply even to error responses)
#   2. CORS
#   3. Request ID / logging
#   4. Request guards (body size cap, Content-Type validation)
#   5. (rate limiting + authentication are per-route FastAPI dependencies,
#      not global middleware -- see src/core/rate_limit.py and the
#      `dependencies=[...]` on individual routes. They run after the layers
#      above and before the route body, which is the same place in the
#      request's lifecycle this ordering calls for.)
#   6. Routes
#
# ...the calls below are written in the REVERSE of that list.
# ---------------------------------------------------------------------------

_supabase_origin = urlparse(settings.supabase_url).scheme + "://" + urlparse(settings.supabase_url).netloc

app.add_middleware(RequestGuardMiddleware)
app.add_middleware(RequestIDMiddleware)

# Configure CORS (Cross-Origin Resource Sharing) middleware to allow secure
# communication with the Next.js frontend client. Methods/headers are the
# real, exhaustive list this API uses -- not a wildcard. Never allow_origins
# with "*", and never combined with allow_credentials=True (which this API
# requires, for the httpOnly session cookie): browsers reject that
# combination outright, and validate_browser_delivery_settings() in
# src/core/config.py also refuses to even boot on it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Added LAST so it is OUTERMOST -- see the ordering note above.
app.add_middleware(SecurityHeadersMiddleware, supabase_origin=_supabase_origin)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Converts any exception that no route/dependency handled into a flat,
    generic 500 -- no stack trace, no internal path, nothing an attacker (or
    an anxious, grieving user staring at an error screen) can learn from the
    response body. The real detail still goes to the server log, keyed by
    the same request id the response carries (see RequestIDMiddleware), so
    it's fully debuggable from there.

    This also happens to be *why* the security-headers middleware can be
    outermost and still simple: Starlette's own fallback for a truly
    unhandled exception bypasses ordinary middleware response processing,
    so without this handler such a response could in principle be the one
    kind of response that misses the headers above. Registering a handler
    means FastAPI's ExceptionMiddleware (which sits just inside this app's
    user middleware stack) produces the response instead, and that DOES
    flow back out through every middleware normally.
    """
    request_id = getattr(request.state, "request_id", None) or request.scope.get("state", {}).get("request_id")
    logging.exception("Unhandled exception (request_id=%s): %s", request_id, exc)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})


@app.get("/", tags=["Root"])
def read_root():
    """Root endpoint verifying that the API service is online."""
    return {"message": "Welcome to the Memoir App API"}


@app.get("/health", tags=["Health"])
def health_check():
    """
    Liveness probe. Deliberately does NOT touch the database.

    An ALB or ECS health check that depends on Postgres will, during a database
    blip, mark every task unhealthy at once and replace all of them -- a
    thundering herd that cannot possibly help, since the replacement tasks need
    the same database. Liveness answers only "is this process wedged", and the
    orchestrator should route on this.

    Use /health/ready for deploy gates and for alerting on data-plane health.
    """
    return {"status": "healthy"}


@app.get("/health/ready", tags=["Health"])
def readiness_check():
    """
    Readiness probe. Verifies the process can actually reach Postgres.

    Returns 503 on failure so a deploy pipeline or a container health check can
    gate on it. The timeout is short and hard-bounded: this runs on the event
    loop, and a health check that hangs is worse than one that fails.

    Deliberately uses `psycopg` directly rather than the app's Supabase client
    or a SQLAlchemy engine -- a pool created per probe would hide connection
    exhaustion and cost more than it measures.
    """
    try:
        import psycopg

        # `database_url_for_psycopg`, not `settings.database_url`. The configured
        # URL carries a SQLAlchemy dialect prefix (`postgresql+psycopg://`) that
        # psycopg rejects outright -- so using it directly makes this probe fail
        # on every environment, and a deploy gate that is always red is a gate
        # people learn to ignore.
        with psycopg.connect(
            database_url_for_psycopg(),
            connect_timeout=READINESS_CONNECT_TIMEOUT_SECONDS,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
    except Exception as exc:
        # Type and message only. A connection string or password can appear in
        # a driver exception's message, and this response is not authenticated.
        logging.warning("readiness check failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unreachable",
        ) from exc

    return {"status": "ready"}


# -----------------------------------------------------------------
# FEATURE ROUTER REGISTRATION
# -----------------------------------------------------------------
app.include_router(auth_router)
app.include_router(memoir_router)
app.include_router(memory_router)
app.include_router(media_router)
app.include_router(comment_router)
app.include_router(search_router)
app.include_router(export_router)
app.include_router(transcript_router)
app.include_router(organization_router)
app.include_router(owner_router)
app.include_router(reader_router)
app.include_router(profile_router)
app.include_router(narrative_router)

# Static review console. Registered last so its `/organize/{memoir_id}` path
# cannot shadow an API route -- and it does not: the API's paths all begin
# `/api/`, and this one has no prefix. Ordered last anyway, because a catch-all
# route added later is the kind of thing that should be an explicit decision.
app.include_router(organize_page_router)