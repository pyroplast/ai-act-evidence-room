"""Shared fixtures. Tests run against a real Postgres 16 (compose locally, a service in CI).

TEST_DATABASE_URL points at any database on that server; the suite creates and migrates
its own throwaway database next to it, so it never touches application data. Each test
runs inside a transaction that is rolled back, which is how rows in append-only tables
disappear again between tests.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from evidence_room.config import DEFAULT_DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
SERVER_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
TEST_DB = "evidence_room_test"


def url_for(database: str) -> str:
    return make_url(SERVER_URL).set(database=database).render_as_string(hide_password=False)


def recreate_database(name: str) -> str:
    admin = create_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        pytest.fail(
            f"Postgres not reachable at {make_url(SERVER_URL).render_as_string()}: {exc.orig}. "
            "Run `make test` (starts the compose db) or set TEST_DATABASE_URL.",
            pytrace=False,
        )
    finally:
        admin.dispose()
    return url_for(name)


def drop_database(name: str) -> None:
    admin = create_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    url = recreate_database(TEST_DB)
    migrate(url)
    yield url
    drop_database(TEST_DB)


TEMPLATE_DB = "evidence_room_test_template"
_fresh_counter = itertools.count(1)


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    """A migrated database used as a template; cloning it is much faster than migrating."""
    migrate(recreate_database(TEMPLATE_DB))
    yield TEMPLATE_DB
    drop_database(TEMPLATE_DB)


@pytest.fixture
def fresh_db_url(template_db: str) -> Iterator[str]:
    """An empty, migrated database of this test's own, for code that commits.

    Rows in append-only tables cannot be deleted, so tests of code that commits (the
    runner, the worker) get a throwaway database instead of a rolled-back transaction.
    """
    name = f"evidence_room_test_fresh_{os.getpid()}_{next(_fresh_counter)}"
    admin = create_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    admin.dispose()
    yield url_for(name)
    drop_database(name)


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back at the end of the test."""
    conn = engine.connect()
    outer = conn.begin()
    sess = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        conn.close()
