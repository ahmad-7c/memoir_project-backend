"""
@file src/db/base.py
@description Declarative base for all SQLAlchemy models. Every model in
src/db/models/ inherits from this so Base.metadata is the single registry
Alembic compares the live database against.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
