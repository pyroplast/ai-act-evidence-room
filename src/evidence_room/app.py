"""FastAPI application. M0: health endpoint only; the review UI lands in M4."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from evidence_room import SCOPE_LINE, __version__
from evidence_room.db import get_engine


def create_app(engine_provider: Callable[[], Engine] = get_engine) -> FastAPI:
    app = FastAPI(title="ai-act-evidence-room", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness + readiness: the database answers and migrations have run."""
        try:
            with engine_provider().connect() as conn:
                conn.execute(text("SELECT 1"))
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        except SQLAlchemyError as exc:
            return JSONResponse(
                {"status": "error", "db": "unreachable", "error": type(exc).__name__},
                status_code=503,
            )
        return JSONResponse(
            {"status": "ok", "db": "ok", "schema": revision, "version": __version__}
        )

    @app.get("/")
    def index() -> dict[str, str]:
        return {"service": "ai-act-evidence-room", "scope": SCOPE_LINE}

    return app


app = create_app()
