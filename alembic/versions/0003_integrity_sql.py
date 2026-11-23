"""Integrity triggers and read-model views from sql/triggers.sql and sql/views.sql.

Both files are idempotent; a later change to either ships with a new migration that
re-applies it (docs/decisions.md, D-005).

Revision ID: 0003_integrity_sql
Revises: 0002_schema
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0003_integrity_sql"
down_revision: str | None = "0002_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def run_sql_file(name: str) -> None:
    """Execute a multi-statement SQL file on the migration's connection and transaction.

    psycopg runs several statements in one call only without bind parameters, so this goes
    to the driver connection directly rather than through SQLAlchemy's text().
    """
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run_sql_file("triggers.sql")
    run_sql_file("views.sql")


def downgrade() -> None:
    op.execute(
        "DROP VIEW IF EXISTS v_matter_summary, v_regressions, v_effective_findings, "
        "v_latest_scan CASCADE"
    )
    for table, triggers in {
        "findings": ("findings_no_update_delete", "findings_no_truncate"),
        "reviews": ("reviews_no_update_delete", "reviews_no_truncate"),
        "audit_log": (
            "audit_log_no_update_delete",
            "audit_log_no_truncate",
            "audit_log_link_guard",
        ),
        "artifacts": ("artifacts_guard", "artifacts_no_truncate"),
    }.items():
        for trigger in triggers:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS evidence_refuse_change()")
    op.execute("DROP FUNCTION IF EXISTS evidence_artifacts_guard()")
    op.execute("DROP FUNCTION IF EXISTS evidence_audit_link_guard()")
