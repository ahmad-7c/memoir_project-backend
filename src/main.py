"""
@file main.py
@description Entry point for the FastAPI application. Bootstraps environment variables, 
configures CORS middleware for frontend integration, defines health check endpoints, 
and mounts all modular feature routers.
"""

import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from src.core.config import settings
from src.api.share import owner_router, reader_router

# CRITICAL: load_dotenv() must be called BEFORE any other application modules 
# are imported so database and storage configurations can read environment variables.
load_dotenv()  

from fastapi import FastAPI
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

def setup_logging():
    """Configures root logging format and log level for backend services."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )


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
    """Health check endpoint utilized by container orchestration and deployment monitors."""
    return {"status": "healthy"}


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