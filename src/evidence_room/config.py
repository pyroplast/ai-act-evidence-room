"""Runtime settings, read from the environment once per process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_DATABASE_URL = "postgresql+psycopg://evidence:evidence@localhost:5433/evidence_room"


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_store: str
    worker_poll_seconds: float


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        artifact_store=os.environ.get("ARTIFACT_STORE", "./store"),
        worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "5")),
    )
