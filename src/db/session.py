"""
@file src/db/session.py
@description SQLAlchemy engine and session factory. Not wired into any request
path yet — the existing repositories keep using the Supabase client. This
exists so Alembic (and future, separate work migrating repositories) has a
single place to get a connection from.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI dependency: hand out a session, guarantee it's closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
