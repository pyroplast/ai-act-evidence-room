"""Engine and session factory."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from evidence_room.config import get_settings


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
