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
    # Base URL for deep links in notifications (the review UI lands in M4).
    app_base_url: str
    # A "running" scan older than this is marked failed (its worker died).
    scan_stale_minutes: int
    # Optional email digest; off unless SMTP_HOST and DIGEST_TO are both set.
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_starttls: bool
    digest_from: str
    digest_to: tuple[str, ...]

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.digest_to)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        artifact_store=os.environ.get("ARTIFACT_STORE", "./store"),
        worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "5")),
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        scan_stale_minutes=int(os.environ.get("SCAN_STALE_MINUTES", "60")),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_starttls=os.environ.get("SMTP_STARTTLS", "true").lower() in ("1", "true", "yes"),
        digest_from=os.environ.get("DIGEST_FROM", "evidence-room@localhost"),
        digest_to=tuple(a.strip() for a in os.environ.get("DIGEST_TO", "").split(",") if a.strip()),
    )
