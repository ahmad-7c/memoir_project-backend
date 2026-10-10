"""
@file main.py
@description Entry point for the FastAPI application. Bootstraps environment variables, 
configures CORS middleware for frontend integration, defines health check endpoints, 
and mounts all modular feature routers.
"""

import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from src.core.config import settings, database_url_for_psycopg
from src.api.share import owner_router, reader_router

# CRITICAL: load_dotenv() must be called BEFORE any other application modules 
# are imported so database and storage configurations can read environment variables.
load_dotenv()  

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

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

# Configure CORS (Cross-Origin Resource Sharing) middleware 
# to allow secure communication with the Next.js frontend client.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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