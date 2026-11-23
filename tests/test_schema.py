"""Migrations and models agree, and the migration chain goes down and up cleanly."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from evidence_room.models import Base
from tests.conftest import alembic_config, drop_database, migrate, recreate_database

TABLES = {
    "users",
    "clients",
    "matters",
    "touchpoints",
    "scans",
    "artifacts",
    "findings",
    "reviews",
    "audit_log",
    "webhooks",
    "deliveries",
}
VIEWS = {"v_latest_scan", "v_effective_findings", "v_regressions", "v_matter_summary"}


def test_every_planned_table_and_view_exists(engine: Engine) -> None:
    insp = inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert set(insp.get_view_names()) >= VIEWS


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models.py and alembic/versions disagree: {diff}"


def test_protection_triggers_are_installed(engine: Engine) -> None:
    with engine.connect() as conn:
        found = set(
            conn.execute(
                text(
                    "SELECT tgrelid::regclass::text || '.' || tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal"
                )
            ).scalars()
        )
    assert {
        "findings.findings_no_update_delete",
        "findings.findings_no_truncate",
        "reviews.reviews_no_update_delete",
        "reviews.reviews_no_truncate",
        "audit_log.audit_log_no_update_delete",
        "audit_log.audit_log_no_truncate",
        "audit_log.audit_log_link_guard",
        "artifacts.artifacts_guard",
        "artifacts.artifacts_no_truncate",
    } <= found


@pytest.fixture
def roundtrip_db() -> Iterator[str]:
    name = "evidence_room_test_roundtrip"
    url = recreate_database(name)
    yield url
    drop_database(name)


def test_downgrade_to_base_and_upgrade_again(roundtrip_db: str) -> None:
    cfg = alembic_config(roundtrip_db)
    migrate(roundtrip_db)
    command.downgrade(cfg, "base")
    engine = create_engine(roundtrip_db)
    try:
        assert not (TABLES & set(inspect(engine).get_table_names()))
        command.upgrade(cfg, "head")
        insp = inspect(engine)
        assert set(insp.get_table_names()) >= TABLES
        assert set(insp.get_view_names()) >= VIEWS
    finally:
        engine.dispose()
