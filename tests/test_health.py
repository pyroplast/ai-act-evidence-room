from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from evidence_room import SCOPE_LINE
from evidence_room.app import create_app


def test_healthz_ok_when_db_is_migrated(engine: Engine) -> None:
    client = TestClient(create_app(lambda: engine))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["schema"]  # an alembic revision id


def test_healthz_503_when_db_unreachable() -> None:
    dead = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )
    client = TestClient(create_app(lambda: dead))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["db"] == "unreachable"


def test_index_carries_scope_line(engine: Engine) -> None:
    client = TestClient(create_app(lambda: engine))
    assert client.get("/").json()["scope"] == SCOPE_LINE
