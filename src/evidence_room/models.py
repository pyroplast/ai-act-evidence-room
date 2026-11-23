"""SQLAlchemy models. The schema itself is owned by Alembic (alembic/versions)."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
